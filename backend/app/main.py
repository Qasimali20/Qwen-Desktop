"""Qwen Desktop API."""
from __future__ import annotations

import logging
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

log = logging.getLogger(__name__)


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
    if n := s.db.encrypt_secrets():
        log.info("encrypted %d stored credentials that were kept in plain text", n)
    yield
    await s.workspaces.stop()
    await s.repos.stop()
    await s.llm.close()


# The OpenAPI schema and docs are served to signed-in admins only (routers/admin.py), not publicly.
app = FastAPI(title="Qwen Desktop", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
for r in (auth, admin, meta, projects, repos, workspaces, prompts, skills, connectors, files, conversations, shares, voice,
          keys, gateway):
    app.include_router(r.router)
app.include_router(repos.by_id)


# Defaults for every response; a route that sets its own (attachments set a sandbox CSP) keeps it.
SECURITY_HEADERS = {
    "X-Frame-Options": "SAMEORIGIN",                      # no framing by other sites (clickjacking)
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    # The mic is used for dictation and voice mode; nothing else needs device access.
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=(), payment=(), usb=()",
    # Narrow, compatibility-safe CSP: blocks framing, plugins, <base> hijacking and off-site form posts without
    # restricting scripts/styles (the artifact panel's sandboxed previews and KaTeX need inline styles).
    "Content-Security-Policy": "frame-ancestors 'self'; object-src 'none'; base-uri 'self'; form-action 'self'",
}


@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
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
