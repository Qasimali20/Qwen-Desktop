"""Code repositories in a project: add (zip upload / git URL), sync, browse, delete."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ..auth import User
from ..repos import normalise_git_url, repo_name_from
from ..state import S

router = APIRouter(prefix="/api/projects/{pid}/repos", tags=["repos"])

MAX_ZIP_BYTES = 300 * 1024 * 1024
_CHUNK = 1024 * 1024


class GitRepoIn(BaseModel):
    url: str = Field(min_length=8, max_length=500)
    ref: str | None = Field(None, max_length=200)
    token: str | None = Field(None, max_length=500)
    name: str | None = Field(None, max_length=60)


def _repo(s, pid: str, rid: str) -> dict:
    r = s.db.get_repo(rid)
    if not r or r["project_id"] != pid:
        raise HTTPException(404, "Repository not found")
    return r


def _require_kb(s) -> None:
    if not s.repos.enabled:
        raise HTTPException(503, "Code indexing needs the embedding model, which is not configured")


async def _save_zip(s, rid: str, file: UploadFile) -> None:
    """Stream the upload to disk in 1 MB pieces (never the whole archive in memory)."""
    dest = s.repos.zip_path(rid)
    size = 0
    try:
        with dest.open("wb") as out:
            while (piece := await file.read(_CHUNK)):
                size += len(piece)
                if size > MAX_ZIP_BYTES:
                    raise HTTPException(413, f"The archive exceeds {MAX_ZIP_BYTES // 2**20} MB. Remove build "
                                             "output and dependencies (node_modules, .venv) and try again.")
                await asyncio.to_thread(out.write, piece)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


@router.get("")
def list_repos(pid: str, user: User, s: S):
    s.project(pid, user)
    return s.db.list_repos(pid)


@router.post("/zip", status_code=201)
async def add_zip(pid: str, user: User, s: S, file: UploadFile = File(...), name: str | None = Form(None)):
    s.project(pid, user, edit=True)
    _require_kb(s)
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(415, "Upload the repository as a .zip archive")
    r = s.db.create_repo(pid, repo_name_from(name or file.filename or "repo"), "zip", user["display_name"])
    try:
        await _save_zip(s, r["id"], file)
    except BaseException:
        s.db.delete_repo(r["id"])
        raise
    s.repos.schedule(r["id"])
    return s.db.get_repo(r["id"])


@router.put("/{rid}/zip")
async def replace_zip(pid: str, rid: str, user: User, s: S, file: UploadFile = File(...)):
    """New version of an uploaded repo: only files that changed are re-embedded."""
    s.project(pid, user, edit=True)
    r = _repo(s, pid, rid)
    if r["source"] != "zip":
        raise HTTPException(400, "This repository is synced from git; use sync instead")
    if s.repos.busy(rid):
        raise HTTPException(409, "This repository is still being indexed")
    await _save_zip(s, rid, file)
    s.db.update_repo(rid, {"status": "queued"})
    s.repos.schedule(rid)
    return s.db.get_repo(rid)


@router.post("/git", status_code=201)
async def add_git(pid: str, body: GitRepoIn, user: User, s: S):
    s.project(pid, user, edit=True)
    _require_kb(s)
    try:
        url, embedded = normalise_git_url(body.url)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    token = (body.token or "").strip() or embedded
    r = s.db.create_repo(pid, repo_name_from(body.name or url), "git", user["display_name"], url=url,
                         ref=(body.ref or "").strip() or None, auth_token=token)
    s.repos.schedule(r["id"])
    return s.db.get_repo(r["id"])


@router.post("/{rid}/sync")
async def sync(pid: str, rid: str, user: User, s: S):
    """git: pull the latest commit (only changed files are re-embedded). Uploaded archive: retry indexing
    after a failure (a new version is uploaded with PUT /zip)."""
    s.project(pid, user, edit=True)
    r = _repo(s, pid, rid)
    if s.repos.busy(rid):
        raise HTTPException(409, "This repository is already syncing")
    if r["source"] == "git":
        s.db.update_repo(rid, {"status": "queued"})
    elif r["status"] == "error" and (r["meta"] or {}).get("files"):
        s.db.update_repo(rid, {"status": "indexing"})
    else:
        raise HTTPException(400, "Upload a new version of the archive to update it")
    s.repos.schedule(rid)   # False only if the other server process picked it up first, which is fine
    return s.db.get_repo(rid)


@router.delete("/{rid}", status_code=204)
async def delete(pid: str, rid: str, user: User, s: S):
    s.project(pid, user, edit=True)
    _repo(s, pid, rid)
    await s.repos.cancel(rid)
    s.db.delete_repo(rid)
    s.kb.invalidate(pid)


@router.get("/{rid}/files")
def files(pid: str, rid: str, user: User, s: S):
    s.project(pid, user)
    _repo(s, pid, rid)
    return s.db.repo_listing(rid)


@router.get("/{rid}/file")
def file(pid: str, rid: str, path: str, user: User, s: S):
    s.project(pid, user)
    _repo(s, pid, rid)
    if not (f := s.db.repo_file(rid, path)):
        raise HTTPException(404, "File not found")
    return f


# Citations in chat carry only the repo id, so the viewer reads files by repo; access follows the project.
by_id = APIRouter(prefix="/api/repos", tags=["repos"])


@by_id.get("/{rid}/file")
def file_by_repo(rid: str, path: str, user: User, s: S):
    r = s.db.get_repo(rid)
    if not r or not s.db.get_project(r["project_id"], user["id"]):
        raise HTTPException(404, "File not found")
    if not (f := s.db.repo_file(rid, path)):
        raise HTTPException(404, "File not found — the repository may have changed since this answer")
    return {**f, "repo": r["name"], "project_id": r["project_id"], "commit_sha": r["commit_sha"]}
