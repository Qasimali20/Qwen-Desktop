"""Read-only share links for conversations.

A link is a snapshot: it shows the conversation as it was when shared (or last updated by its owner).
Anyone signed in to Qwen Desktop who has the link can view it; the owner can update or revoke it.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException

from ..auth import User
from ..state import S, public_attachment

router = APIRouter(tags=["shares"])

# Message fields kept in a snapshot (usage/costs and internal metadata stay private).
_MSG_FIELDS = ("id", "role", "content", "blocks", "attachment_ids", "model_id", "created_at")


def _snapshot(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        snap = {k: m[k] for k in _MSG_FIELDS}
        if (prompt := (m.get("meta") or {}).get("prompt")):
            snap["meta"] = {"prompt": prompt}
        if m["role"] == "assistant" and (u := m.get("usage")) and u.get("error"):
            snap["usage"] = {"error": u["error"]}
        out.append(snap)
    return out


@router.get("/api/conversations/{cid}/share")
def get_share(cid: str, user: User, s: S):
    s.conversation(cid, user)
    return s.db.share_of(cid)


@router.post("/api/conversations/{cid}/share")
def save_share(cid: str, user: User, s: S):
    """Creates the link, or refreshes it to the conversation's current state (same URL)."""
    conv = s.conversation(cid, user)
    msgs = s.db.messages(cid)
    if not msgs:
        raise HTTPException(400, "Nothing to share yet")
    share, orphaned = s.db.save_share(secrets.token_urlsafe(18), cid, user["id"], conv["title"], _snapshot(msgs))
    s.unlink(orphaned)
    return share


@router.delete("/api/conversations/{cid}/share", status_code=204)
def delete_share(cid: str, user: User, s: S):
    s.conversation(cid, user)
    s.unlink(s.db.delete_share(cid))


@router.get("/api/shares")
def list_shares(user: User, s: S):
    return s.db.list_shares(user["id"])


@router.get("/api/shares/{sid}")
def view_share(sid: str, user: User, s: S):
    share = s.db.get_share(sid, with_messages=True)
    if not share:
        raise HTTPException(404, "This link does not exist or has been removed")
    owner = s.db.get_user(share["user_id"])
    msgs = share.pop("messages")
    atts = s.db.get_attachments([a for m in msgs for a in m["attachment_ids"]])
    for m in msgs:
        m["attachments"] = [public_attachment(atts[a]) for a in m["attachment_ids"] if a in atts]
    is_owner = share["user_id"] == user["id"]
    if not is_owner:
        s.db.count_share_view(sid)
    return {**share, "messages": msgs, "owner": owner["display_name"] if owner else "Unknown",
            "is_owner": is_owner}
