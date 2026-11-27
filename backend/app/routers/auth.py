from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import audit, auth
from ..auth import User
from ..state import S

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordIn(BaseModel):
    current: str
    new: str = Field(max_length=256)


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response, s: S):
    return await auth.login(s.db, s.throttle, request, response, body.username, body.password)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, s: S):
    auth.logout(s.db, request, response)


@router.get("/me")
def me(user: User):
    return user


@router.post("/password", status_code=204)
async def change_password(body: PasswordIn, request: Request, user: User, s: S):
    if not await asyncio.to_thread(auth.verify_password, body.current, s.db.password_hash(user["id"]) or ""):
        raise HTTPException(400, "Current password is incorrect")
    auth.validate_password(body.new)
    s.db.update_user(user["id"], {"password_hash": await asyncio.to_thread(auth.hash_password, body.new)})
    audit.record(s.db, request, user, "auth.password_changed", target_type="user", target_id=user["id"],
                 target_label=user["username"])
    # Sign out every other device.
    s.db.delete_user_sessions(user["id"], keep=auth.session_digest(request))
