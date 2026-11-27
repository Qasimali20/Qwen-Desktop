from __future__ import annotations

import asyncio
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit, auth
from ..auth import Admin
from ..state import S

router = APIRouter(prefix="/api/admin", tags=["admin"])

USERNAME = r"^[A-Za-z0-9._-]{2,32}$"


class UserIn(BaseModel):
    username: str = Field(pattern=USERNAME)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(max_length=256)
    role: Literal["admin", "user"] = "user"


class UserPatch(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=80)
    role: Literal["admin", "user"] | None = None
    disabled: bool | None = None
    password: str | None = Field(None, max_length=256)


@router.get("/users")
def list_users(_: Admin, s: S):
    return s.db.list_users()


@router.post("/users")
async def create_user(body: UserIn, me: Admin, s: S, request: Request):
    auth.validate_password(body.password)
    if s.db.get_user_auth(body.username):
        raise HTTPException(409, "Username already exists")
    pw = await asyncio.to_thread(auth.hash_password, body.password)
    user = s.db.create_user(body.username, body.display_name, pw, body.role)
    audit.record(s.db, request, me, "user.created", target_type="user", target_id=user["id"],
                 target_label=user["username"], role=body.role)
    return user


def _guard_last_admin(s, uid: str) -> None:
    admins = [u for u in s.db.list_users() if u["role"] == "admin" and not u["disabled"]]
    if len(admins) == 1 and admins[0]["id"] == uid:
        raise HTTPException(400, "You cannot remove the last active admin")


@router.patch("/users/{uid}")
async def update_user(uid: str, body: UserPatch, me: Admin, s: S, request: Request):
    if not (before := s.db.get_user(uid)):
        raise HTTPException(404, "User not found")
    fields = body.model_dump(exclude_none=True)
    if fields.get("role") == "user" or fields.get("disabled"):
        _guard_last_admin(s, uid)
    if "password" in fields:
        auth.validate_password(fields["password"])
        fields["password_hash"] = await asyncio.to_thread(auth.hash_password, fields.pop("password"))
    if "disabled" in fields:
        fields["disabled"] = int(fields["disabled"])
    user = s.db.update_user(uid, fields)
    if fields.get("disabled") or "password_hash" in fields:
        s.db.delete_user_sessions(uid)  # force re-login / lock out immediately
    changes = {k: [before[k], user[k]] for k in ("role", "display_name") if k in fields and before[k] != user[k]}
    if "disabled" in fields and bool(before["disabled"]) != bool(user["disabled"]):
        changes["disabled"] = [bool(before["disabled"]), bool(user["disabled"])]
    if changes or "password_hash" in fields:
        audit.record(s.db, request, me, "user.updated", target_type="user", target_id=uid, target_label=user["username"],
                     changes=changes or None, password_reset=True if "password_hash" in fields else None)
    return user


@router.delete("/users/{uid}", status_code=204)
def delete_user(uid: str, me: Admin, s: S, request: Request):
    if uid == me["id"]:
        raise HTTPException(400, "You cannot delete your own account")
    if not (victim := s.db.get_user(uid)):
        raise HTTPException(404, "User not found")
    _guard_last_admin(s, uid)
    conv_ids = [c["id"] for c in s.db.list_conversations(uid, limit=100_000)]
    project_ids = s.db.owned_project_ids(uid)
    s.unlink(s.db.delete_user(uid))
    audit.record(s.db, request, me, "user.deleted", target_type="user", target_id=uid, target_label=victim["username"],
                 display_name=victim["display_name"], role=victim["role"], chats=len(conv_ids), projects=len(project_ids))
    for cid in conv_ids:
        s.sandbox.drop(cid)
    for pid in project_ids:
        s.kb.invalidate(pid)


@router.get("/activity")
def activity(_: Admin, s: S, limit: int = 100, before: int | None = None, category: str | None = None,
             q: str | None = None):
    limit = max(1, min(limit, 500))
    items = s.db.list_audit(limit=limit + 1, before=before, category=category or None, q=(q or "").strip()[:100] or None)
    return {"items": items[:limit], "next": items[limit - 1]["id"] if len(items) > limit else None}


@router.get("/usage")
def usage(_: Admin, s: S, days: int = 30):
    days = max(1, min(days, 365))
    return {"days": days, **s.db.usage(time.time() - days * 86_400)}
