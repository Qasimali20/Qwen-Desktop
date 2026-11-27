"""Project workspaces: create from a repository (or empty), review changes, download, commit and push."""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from ..auth import User
from ..state import S
from ..workspaces import MAX_DIFF_CHARS, WorkspaceError

router = APIRouter(tags=["workspaces"])


class WorkspaceIn(BaseModel):
    name: str | None = Field(None, max_length=60)
    repo_id: str | None = None


class DiscardIn(BaseModel):
    path: str | None = None


class CommitIn(BaseModel):
    message: str = Field("", max_length=5000)
    push: bool = False
    branch: str | None = Field(None, max_length=120)


def public(s, ws: dict) -> dict:
    repo = s.db.get_repo(ws["repo_id"]) if ws.get("repo_id") else None
    return {**ws, "repo": {"id": repo["id"], "name": repo["name"], "source": repo["source"], "url": repo.get("url")}
            if repo else None, "can_push": bool(repo and repo["source"] == "git")}


def owned(s, wid: str, user: dict, ready: bool = True) -> dict:
    ws = s.db.get_workspace(wid)
    if not ws or ws["user_id"] != user["id"]:
        raise HTTPException(404, "Workspace not found")
    if ready and ws["status"] != "ready":
        raise HTTPException(409, "The workspace is not ready")
    return ws


def _slug(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "-", name).strip("-.")[:60] or "workspace"


@router.get("/api/projects/{pid}/workspaces")
def list_workspaces(pid: str, user: User, s: S):
    s.project(pid, user)
    return [public(s, w) for w in s.db.list_workspaces(pid, user["id"])]


@router.post("/api/projects/{pid}/workspaces", status_code=201)
async def create_workspace(pid: str, body: WorkspaceIn, user: User, s: S):
    s.project(pid, user)
    repo = None
    if body.repo_id:
        repo = s.db.get_repo(body.repo_id)
        if not repo or repo["project_id"] != pid:
            raise HTTPException(404, "Repository not found")
        if repo["source"] == "zip" and not (repo["meta"] or {}).get("files"):
            raise HTTPException(409, "The repository has not been indexed yet")
        if repo["source"] == "git" and not repo.get("commit_sha"):
            raise HTTPException(409, "The repository has not been fetched yet")
    name = _slug(body.name or (repo["name"] if repo else "workspace"))
    try:
        ws = s.workspaces.create(pid, user, name, repo)
    except WorkspaceError as e:
        raise HTTPException(409, str(e)) from e
    return public(s, ws)


@router.get("/api/workspaces/{wid}")
def get_workspace(wid: str, user: User, s: S):
    return public(s, owned(s, wid, user, ready=False))


@router.delete("/api/workspaces/{wid}", status_code=204)
async def delete_workspace(wid: str, user: User, s: S):
    owned(s, wid, user, ready=False)
    await s.workspaces.delete(wid)


def _fail(e: WorkspaceError) -> HTTPException:
    return HTTPException(400, str(e))


@router.get("/api/workspaces/{wid}/changes")
async def changes(wid: str, user: User, s: S):
    owned(s, wid, user)
    try:
        return await s.workspaces.changes(wid)
    except WorkspaceError as e:
        raise _fail(e) from e


@router.get("/api/workspaces/{wid}/diff")
async def diff(wid: str, user: User, s: S, path: str | None = None):
    owned(s, wid, user)
    try:
        text = await s.workspaces.diff(wid, path)
    except WorkspaceError as e:
        raise _fail(e) from e
    return {"diff": text[:MAX_DIFF_CHARS], "truncated": len(text) > MAX_DIFF_CHARS}


@router.get("/api/workspaces/{wid}/patch")
async def patch(wid: str, user: User, s: S):
    ws = owned(s, wid, user)
    try:
        text = await s.workspaces.diff(wid, binary=True)
    except WorkspaceError as e:
        raise _fail(e) from e
    return Response(text, media_type="text/x-diff",
                    headers={"Content-Disposition": f'attachment; filename="{ws["name"]}.patch"'})


@router.get("/api/workspaces/{wid}/archive")
async def archive(wid: str, user: User, s: S):
    ws = owned(s, wid, user)
    try:
        path = await s.workspaces.archive(wid)
    except WorkspaceError as e:
        raise _fail(e) from e
    return FileResponse(path, media_type="application/zip", filename=f"{ws['name']}.zip",
                        background=BackgroundTask(path.unlink, missing_ok=True))


@router.post("/api/workspaces/{wid}/discard")
async def discard(wid: str, body: DiscardIn, user: User, s: S):
    owned(s, wid, user)
    try:
        await s.workspaces.discard(wid, body.path)
    except WorkspaceError as e:
        raise _fail(e) from e
    return await s.workspaces.changes(wid)


@router.post("/api/workspaces/{wid}/commit")
async def commit(wid: str, body: CommitIn, user: User, s: S):
    owned(s, wid, user)
    try:
        sha = await s.workspaces.commit(wid, body.message, user)
        pushed = await s.workspaces.push(wid, body.branch or "") if body.push else None
    except WorkspaceError as e:
        raise _fail(e) from e
    return {"sha": sha, "pushed": pushed, "workspace": public(s, s.db.get_workspace(wid))}


@router.post("/api/workspaces/{wid}/push")
async def push(wid: str, body: CommitIn, user: User, s: S):
    """Pushes already-committed work (e.g. after a failed push) without making a new commit."""
    owned(s, wid, user)
    try:
        pushed = await s.workspaces.push(wid, body.branch or "")
    except WorkspaceError as e:
        raise _fail(e) from e
    return {"pushed": pushed, "workspace": public(s, s.db.get_workspace(wid))}
