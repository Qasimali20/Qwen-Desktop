from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ..auth import User
from ..state import S, project_permissions, public_attachment
from .files import ingest

TeamAccess = Literal["none", "view", "edit"]

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field("", max_length=2000)
    instructions: str = Field("", max_length=100_000)
    team_access: TeamAccess = "none"


class ProjectPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    instructions: str | None = Field(None, max_length=100_000)
    team_access: TeamAccess | None = None


def _with_permissions(p: dict, user: dict) -> dict:
    return {**p, "permissions": project_permissions(p, user)}


@router.get("")
def list_projects(user: User, s: S):
    return [_with_permissions(p, user) for p in s.db.list_projects(user["id"])]


@router.post("")
def create_project(body: ProjectIn, user: User, s: S):
    p = s.db.create_project(user["id"], body.name, body.description, body.instructions, body.team_access)
    return _with_permissions(p, user)


@router.get("/{pid}")
def get_project(pid: str, user: User, s: S):
    p = s.project(pid, user)
    files = [public_attachment(f) for f in s.db.project_files(pid)]
    return {**p, "files": files, "repos": s.db.list_repos(pid),
            "conversations": s.db.list_conversations(user["id"], project_id=pid)}


@router.patch("/{pid}")
def patch_project(pid: str, body: ProjectPatch, user: User, s: S):
    fields = body.model_dump(exclude_none=True)
    s.project(pid, user, edit=True, manage=bool({"name", "team_access"} & fields.keys()))
    s.db.update_project(pid, fields)
    return s.project(pid, user)


@router.delete("/{pid}", status_code=204)
async def delete_project(pid: str, user: User, s: S):
    s.project(pid, user, manage=True)
    for r in s.db.list_repos(pid):
        await s.repos.cancel(r["id"])
    workspaces = s.db.all_project_workspaces(pid)
    s.unlink(s.db.delete_project(pid))
    for wid in workspaces:   # every member's workspaces go with the project
        await s.workspaces.delete(wid)
    s.kb.invalidate(pid)


@router.post("/{pid}/files")
async def upload_project_file(pid: str, user: User, s: S, file: UploadFile = File(...)):
    p = s.project(pid, user, edit=True)
    # Project files belong to the project owner (so they live and die with the project); the
    # uploader is recorded for attribution.
    att = await ingest(s, user, file, pid, owner_id=p["user_id"])
    s.db.update_project(pid, {})
    if att["kind"] == "document":
        s.kb.schedule(att)
        att["meta"] = {**att["meta"], "kb": {"status": "indexing"}}
    return public_attachment(att)


@router.delete("/{pid}/files/{aid}", status_code=204)
def delete_project_file(pid: str, aid: str, user: User, s: S):
    s.project(pid, user, edit=True)
    att = s.db.get_attachment(aid, with_text=False)
    if not att or att["project_id"] != pid:
        raise HTTPException(404, "File not found")
    s.unlink([p for p in [s.db.delete_attachment(aid)] if p])
    s.kb.invalidate(pid)
