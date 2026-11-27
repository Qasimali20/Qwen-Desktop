"""Qwen Desktop API."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import ROOT, get_settings
from .prompts import seed_defaults
from . import audit
from .routers import (admin, auth, connectors, conversations, files, gateway, keys, meta, projects, prompts, repos,
                      shares, skills, voice, workspaces)
from .state import State


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.s = s = State(get_settings())
    s.unlink(s.db.purge_unsent_uploads())
    s.db.purge_expired_sessions()
    seed_defaults(s.db)
    await s.kb.resume_pending()
    await s.sandbox.probe()
    await s.sandbox.cleanup_orphans()
    s.repos.start()   # also resumes index jobs interrupted by a restart
    s.workspaces.start()
    audit.prune(s.db)
    yield
    await s.workspaces.stop()
    await s.repos.stop()
    await s.llm.close()


app = FastAPI(title="Qwen Desktop", lifespan=lifespan)
for r in (auth, admin, meta, projects, repos, workspaces, prompts, skills, connectors, files, conversations, shares, voice,
          keys, gateway):
    app.include_router(r.router)
app.include_router(repos.by_id)


@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


# ---------------------------------------------------------------- frontend (production build)
DIST = ROOT / "frontend" / "dist"
if DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith(("api/", "v1/")):
            raise HTTPException(404)
        f = (DIST / path).resolve()
        if path and f.is_file() and DIST.resolve() in f.parents:
            return FileResponse(f)
        return FileResponse(DIST / "index.html", headers={"Cache-Control": "no-cache"})
