from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit
from ..auth import User
from ..prompts import COMMAND_RE
from ..state import S

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


class PromptIn(BaseModel):
    command: str = Field(pattern=COMMAND_RE)
    title: str = Field(min_length=1, max_length=80)
    description: str = Field("", max_length=200)
    body: str = Field(min_length=1, max_length=20_000)
    scope: Literal["personal", "team"] = "personal"


class PromptPatch(BaseModel):
    command: str | None = Field(None, pattern=COMMAND_RE)
    title: str | None = Field(None, min_length=1, max_length=80)
    description: str | None = Field(None, max_length=200)
    body: str | None = Field(None, min_length=1, max_length=20_000)


def _public(p: dict, user: dict) -> dict:
    team = p["owner_id"] is None
    return {**{k: v for k, v in p.items() if k != "owner_id"}, "scope": "team" if team else "personal",
            "editable": user["role"] == "admin" if team else True}


def _editable(s, prompt_id: str, user: dict) -> dict:
    p = s.db.get_prompt(prompt_id, user["id"])
    if not p:
        raise HTTPException(404, "Shortcut not found")
    if p["owner_id"] is None and user["role"] != "admin":
        raise HTTPException(403, "Only admins can change team shortcuts")
    return p


@router.get("")
def list_prompts(user: User, s: S):
    return [_public(p, user) for p in s.db.list_prompts(user["id"])]


@router.post("")
def create_prompt(body: PromptIn, user: User, s: S, request: Request):
    owner = None if body.scope == "team" else user["id"]
    if owner is None and user["role"] != "admin":
        raise HTTPException(403, "Only admins can create team shortcuts")
    if s.db.prompt_command_taken(owner, body.command):
        raise HTTPException(409, f"/{body.command} already exists")
    p = s.db.create_prompt(owner, body.command, body.title, body.description, body.body)
    if owner is None:
        audit.record(s.db, request, user, "shortcut.created", target_type="shortcut", target_id=p["id"],
                     target_label=f"/{p['command']}")
    return _public(p, user)


@router.patch("/{prompt_id}")
def update_prompt(prompt_id: str, body: PromptPatch, user: User, s: S, request: Request):
    p = _editable(s, prompt_id, user)
    fields = body.model_dump(exclude_none=True)
    if "command" in fields and s.db.prompt_command_taken(p["owner_id"], fields["command"], exclude=prompt_id):
        raise HTTPException(409, f"/{fields['command']} already exists")
    updated = s.db.update_prompt(prompt_id, fields)
    if p["owner_id"] is None:
        audit.record(s.db, request, user, "shortcut.updated", target_type="shortcut", target_id=prompt_id,
                     target_label=f"/{updated['command']}", fields=sorted(k for k in fields if fields[k] != p[k]) or None)
    return _public(updated, user)


@router.delete("/{prompt_id}", status_code=204)
def delete_prompt(prompt_id: str, user: User, s: S, request: Request):
    p = _editable(s, prompt_id, user)
    s.db.delete_prompt(prompt_id)
    if p["owner_id"] is None:
        audit.record(s.db, request, user, "shortcut.deleted", target_type="shortcut", target_id=prompt_id,
                     target_label=f"/{p['command']}")
