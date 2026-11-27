"""Authentication: scrypt password hashes, server-side sessions in an HttpOnly cookie.

* Passwords: scrypt (memory-hard, stdlib) with per-user salt; constant-time compare.
* Sessions: 256-bit random token in the cookie, only its SHA-256 is stored, so a
  database leak does not leak usable sessions. Sliding 30-day expiry.
* Login throttling: per-IP and per-username failure windows.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response

from . import audit
from .db import Database

COOKIE = "qd_session"
SESSION_TTL = 30 * 86_400
TOUCH_EVERY = 600                 # refresh sliding expiry at most every 10 min
MIN_PASSWORD = 8
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}

MAX_FAILURES_USER = 5     # per username: stops password guessing on one account
MAX_FAILURES_IP = 20      # per client IP: offices share NAT addresses, so this is looser
FAILURE_WINDOW = 15 * 60


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(dk)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(expected))
        return hmac.compare_digest(got, expected)
    except (ValueError, TypeError):
        return False


# A real hash to compare against when the username does not exist, so response
# timing does not reveal which usernames are valid.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise HTTPException(400, f"Password must be at least {MIN_PASSWORD} characters")


# ---------------------------------------------------------------- throttling
class LoginThrottle:
    def __init__(self) -> None:
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str) -> deque[float]:
        q = self._fails[key]
        cutoff = time.monotonic() - FAILURE_WINDOW
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def check(self, *keys: str) -> None:
        for k in keys:
            q = self._recent(k)
            if len(q) >= (MAX_FAILURES_IP if k.startswith("ip:") else MAX_FAILURES_USER):
                wait = int(FAILURE_WINDOW - (time.monotonic() - q[0])) // 60 + 1
                raise HTTPException(429, f"Too many failed attempts. Try again in {wait} min.")

    def fail(self, *keys: str) -> None:
        for k in keys:
            self._recent(k).append(time.monotonic())

    def reset(self, *keys: str) -> None:
        for k in keys:
            self._fails.pop(k, None)


# ---------------------------------------------------------------- sessions
def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def login(db: Database, throttle: LoginThrottle, request: Request, response: Response,
                username: str, password: str) -> dict:
    ip = request.client.host if request.client else "?"
    keys = (f"ip:{ip}", f"user:{username.lower()}")
    throttle.check(*keys)
    row = db.get_user_auth(username.strip())
    stored = row["password_hash"] if row else _DUMMY_HASH
    ok = await asyncio.to_thread(verify_password, password, stored)
    if not row or not ok or row["disabled"]:
        throttle.fail(*keys)
        reason = "unknown user" if not row else "wrong password" if not ok else "account disabled"
        audit.record(db, request, row and {"id": row["id"], "username": row["username"]}, "auth.login_failed",
                     actor_name=(row["username"] if row else username.strip()[:64]) or "?", reason=reason)
        raise HTTPException(401, "Incorrect username or password")
    throttle.reset(*keys)
    audit.record(db, request, {"id": row["id"], "username": row["username"]}, "auth.login")
    token = secrets.token_urlsafe(32)
    db.create_session(_digest(token), row["id"], SESSION_TTL, ip, request.headers.get("user-agent", "")[:300])
    set_cookie(request, response, token)
    return db.get_user(row["id"])


def set_cookie(request: Request, response: Response, token: str) -> None:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax", secure=secure, path="/")


def logout(db: Database, request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        db.delete_session(_digest(token))
    response.delete_cookie(COOKIE, path="/")


def session_digest(request: Request) -> str | None:
    token = request.cookies.get(COOKIE)
    return _digest(token) if token else None


def resolve_user(db: Database, request: Request) -> dict | None:
    digest = session_digest(request)
    if not digest:
        return None
    user = db.session_user(digest)
    if user and time.time() - user.pop("session_seen") > TOUCH_EVERY:
        db.touch_session(digest, SESSION_TTL)
    return user


# ---------------------------------------------------------------- dependencies
def _db(request: Request) -> Database:
    return request.app.state.s.db


def current_user(request: Request, db: Annotated[Database, Depends(_db)]) -> dict:
    user = resolve_user(db, request)
    if not user:
        raise HTTPException(401, "Not signed in")
    return user


def require_admin(user: Annotated[dict, Depends(current_user)]) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only")
    return user


User = Annotated[dict, Depends(current_user)]
Admin = Annotated[dict, Depends(require_admin)]
