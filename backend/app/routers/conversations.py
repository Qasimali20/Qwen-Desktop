from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth import User
from ..chat import ChatOptions, sse
from ..prompts import snapshot
from ..state import S, public_attachment

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


class ConversationIn(BaseModel):
    model_id: str | None = None
    project_id: str | None = None
    workspace_id: str | None = None


class ConversationPatch(BaseModel):
    title: str | None = Field(None, max_length=200)
    model_id: str | None = None
    pinned: bool | None = None
    system_prompt: str | None = None
    workspace_id: str | None = None   # attach the chat to one of your workspaces in its project (null: detach)


class SendIn(BaseModel):
    content: str = ""
    attachment_ids: list[str] = []
    model_id: str | None = None
    thinking: bool = False
    web_search: bool = False
    analysis: bool = True
    # Prompt shortcut (/command) applied to this message's text.
    prompt_id: str | None = None
    # Skill (expert playbook) the user picked for this message.
    skill_id: str | None = None
    # MCP connectors (server ids) switched on for this reply.
    connectors: list[str] = Field(default_factory=list, max_length=20)
    # Edit: replace this user message (and everything after it). Regenerate: re-answer
    # the last user message, discarding everything after it.
    edit_message_id: str | None = None
    regenerate: bool = False


def _check_workspace(s, wid: str | None, project_id: str | None, user: dict) -> None:
    if wid is None:
        return
    ws = s.db.get_workspace(wid)
    if not ws or ws["user_id"] != user["id"] or ws["project_id"] != project_id:
        raise HTTPException(404, "Workspace not found in this project")


def _workspace_brief(s, conv: dict) -> dict | None:
    ws = s.db.get_workspace(conv["workspace_id"]) if conv.get("workspace_id") else None
    return {"id": ws["id"], "name": ws["name"], "status": ws["status"]} if ws else None


@router.get("")
def list_conversations(user: User, s: S, q: str | None = None, project_id: str | None = None, limit: int = 200):
    return s.db.list_conversations(user["id"], q=q, project_id=project_id, limit=min(limit, 1000))


@router.post("")
def create_conversation(body: ConversationIn, user: User, s: S):
    if body.project_id:
        s.project(body.project_id, user)
    _check_workspace(s, body.workspace_id, body.project_id, user)
    conv = s.db.create_conversation(user["id"], body.model_id, body.project_id)
    if body.workspace_id:
        conv = s.db.update_conversation(conv["id"], {"workspace_id": body.workspace_id})
    return {**conv, "workspace": _workspace_brief(s, conv)}


@router.get("/{cid}")
def get_conversation(cid: str, user: User, s: S):
    conv = s.conversation(cid, user)
    msgs = s.db.messages(cid)
    atts = s.db.get_attachments([a for m in msgs for a in m["attachment_ids"]])
    for m in msgs:
        m["attachments"] = [public_attachment(atts[a]) for a in m["attachment_ids"] if a in atts]
    project = s.db.get_project(conv["project_id"], user["id"]) if conv["project_id"] else None
    return {**conv, "messages": msgs, "project": project, "workspace": _workspace_brief(s, conv)}


@router.patch("/{cid}")
def patch_conversation(cid: str, body: ConversationPatch, user: User, s: S):
    conv = s.conversation(cid, user)
    fields = body.model_dump(exclude_unset=True)
    if "workspace_id" in fields:
        _check_workspace(s, fields["workspace_id"], conv["project_id"], user)
    if "pinned" in fields:
        fields["pinned"] = int(bool(fields["pinned"]))
    return s.db.update_conversation(cid, fields)


@router.delete("/{cid}", status_code=204)
def delete_conversation(cid: str, user: User, s: S):
    s.conversation(cid, user)
    s.unlink(s.db.delete_conversation(cid))
    s.sandbox.drop(cid)


@router.post("/{cid}/messages")
async def send_message(cid: str, body: SendIn, user: User, s: S):
    conv = s.conversation(cid, user)
    s.own_attachments(body.attachment_ids, user)
    msgs = s.db.messages(cid)

    if body.regenerate:
        last_user = next((m for m in reversed(msgs) if m["role"] == "user"), None)
        if not last_user:
            raise HTTPException(400, "Nothing to regenerate")
        s.unlink(s.db.truncate_from(cid, last_user["seq"] + 1))
        user_msg = None
    else:
        meta = {}
        if body.prompt_id:
            if not (prompt := s.db.get_prompt(body.prompt_id, user["id"])):
                raise HTTPException(404, "Shortcut not found")
            meta["prompt"] = snapshot(prompt)
        if body.skill_id:
            if not (skill := s.skills.get(user["id"], body.skill_id)):
                raise HTTPException(404, "Skill not found")
            meta["skill"] = skill.snapshot()
        if not body.content.strip() and not body.attachment_ids and not meta:
            raise HTTPException(400, "Empty message")
        if body.edit_message_id:
            target = next((m for m in msgs if m["id"] == body.edit_message_id and m["role"] == "user"), None)
            if not target:
                raise HTTPException(404, "Message to edit not found")
            # Editing the text keeps the shortcut the message was sent with.
            meta = meta or {k: v for k, v in (target.get("meta") or {}).items() if k in ("prompt", "skill")}
            user_msg, orphaned = s.db.replace_from(cid, target["seq"], body.content, body.attachment_ids, meta)
            s.unlink(orphaned)
        else:
            user_msg = s.db.add_message(cid, "user", body.content, attachment_ids=body.attachment_ids, meta=meta)

    if body.model_id and body.model_id != conv["model_id"]:
        conv = s.db.update_conversation(cid, {"model_id": body.model_id})

    opts = ChatOptions(model_id=body.model_id, thinking=body.thinking, web_search=body.web_search,
                       analysis=body.analysis, connectors=body.connectors)

    async def stream():
        if user_msg:
            atts = s.db.get_attachments(user_msg["attachment_ids"])
            user_msg["attachments"] = [public_attachment(atts[a]) for a in user_msg["attachment_ids"] if a in atts]
            yield sse("user_message", message=user_msg)
        async for chunk in s.chat.respond(conv, opts):
            yield chunk

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
