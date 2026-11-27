"""Personal API keys for the gateway, plus the admin view over every key."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit
from ..auth import Admin, User
from ..gateway import new_key
from ..state import S

router = APIRouter(prefix="/api", tags=["api-keys"])


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@router.get("/keys")
def list_keys(user: User, s: S):
    return s.db.list_api_keys(user["id"])


@router.post("/keys")
def create_key(body: KeyIn, user: User, s: S):
    limit = s.settings.gateway.max_keys_per_user
    if s.db.count_active_keys(user["id"]) >= limit:
        raise HTTPException(400, f"You can have at most {limit} active keys. Revoke one first.")
    key, prefix, digest = new_key()
    row = s.db.create_api_key(user["id"], body.name.strip(), prefix, digest)
    return {**row, "key": key}  # the only time the full key is ever returned


@router.delete("/keys/{kid}", status_code=204)
def revoke_key(kid: str, user: User, s: S):
    if not s.db.revoke_api_key(kid, user["id"]):
        raise HTTPException(404, "Key not found")


@router.get("/admin/keys")
def admin_list_keys(_: Admin, s: S):
    return s.db.list_api_keys()


@router.delete("/admin/keys/{kid}", status_code=204)
def admin_revoke_key(kid: str, me: Admin, s: S, request: Request):
    key = next((k for k in s.db.list_api_keys() if k["id"] == kid), None)
    if not s.db.revoke_api_key(kid):
        raise HTTPException(404, "Key not found")
    audit.record(s.db, request, me, "api_key.revoked", target_type="api_key", target_id=kid,
                 target_label=key and f"{key['username']} / {key['name']}", prefix=key and key["prefix"])
