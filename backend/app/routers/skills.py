from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit
from ..auth import User
from ..skills import MAX_BODY, MAX_DESCRIPTION, NAME_RE, Skill
from ..state import S

router = APIRouter(prefix="/api/skills", tags=["skills"])


class SkillIn(BaseModel):
    name: str = Field(pattern=NAME_RE)
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=10, max_length=MAX_DESCRIPTION)
    body: str = Field(min_length=20, max_length=MAX_BODY)
    scope: Literal["personal", "team"] = "personal"


class SkillPatch(BaseModel):
    name: str | None = Field(None, pattern=NAME_RE)
    title: str | None = Field(None, min_length=1, max_length=80)
    description: str | None = Field(None, min_length=10, max_length=MAX_DESCRIPTION)
    body: str | None = Field(None, min_length=20, max_length=MAX_BODY)


def _public(sk: Skill, user: dict) -> dict:
    editable = sk.scope == "personal" or (sk.scope == "team" and user["role"] == "admin")
    return sk.public(editable=editable)


def _check_name(s, owner: str | None, name: str, exclude: str | None = None) -> None:
    if name in s.skills.builtin_names():
        raise HTTPException(409, f"'{name}' is a built-in skill; choose another name")
    if s.db.skill_name_taken(owner, name, exclude=exclude):
        raise HTTPException(409, f"A skill named '{name}' already exists")


def _editable(s, skill_id: str, user: dict) -> dict:
    r = s.db.get_skill(skill_id, user["id"])
    if not r:
        raise HTTPException(404 if not skill_id.startswith("builtin:") else 403,
                            "Built-in skills can't be changed; duplicate one instead" if skill_id.startswith("builtin:")
                            else "Skill not found")
    if r["owner_id"] is None and user["role"] != "admin":
        raise HTTPException(403, "Only admins can change team skills")
    return r


@router.get("")
def list_skills(user: User, s: S):
    return [_public(sk, user) for sk in s.skills.for_user(user["id"])]


@router.post("", status_code=201)
def create_skill(body: SkillIn, user: User, s: S, request: Request):
    owner = None if body.scope == "team" else user["id"]
    if owner is None and user["role"] != "admin":
        raise HTTPException(403, "Only admins can create team skills")
    _check_name(s, owner, body.name)
    r = s.db.create_skill(owner, body.name, body.title.strip(), body.description.strip(), body.body.strip())
    if owner is None:
        audit.record(s.db, request, user, "skill.created", target_type="skill", target_id=r["id"], target_label=r["title"],
                     name=r["name"])
    return _public(s.skills.get(user["id"], r["id"]), user)


@router.patch("/{skill_id}")
def update_skill(skill_id: str, body: SkillPatch, user: User, s: S, request: Request):
    r = _editable(s, skill_id, user)
    fields = {k: v.strip() for k, v in body.model_dump(exclude_none=True).items()}
    if "name" in fields and fields["name"] != r["name"]:
        _check_name(s, r["owner_id"], fields["name"], exclude=skill_id)
    s.db.update_skill(skill_id, fields)
    if r["owner_id"] is None:
        audit.record(s.db, request, user, "skill.updated", target_type="skill", target_id=skill_id,
                     target_label=fields.get("title", r["title"]), fields=sorted(k for k in fields if fields[k] != r[k]) or None)
    return _public(s.skills.get(user["id"], skill_id), user)


@router.delete("/{skill_id}", status_code=204)
def delete_skill(skill_id: str, user: User, s: S, request: Request):
    r = _editable(s, skill_id, user)
    s.db.delete_skill(skill_id)
    if r["owner_id"] is None:
        audit.record(s.db, request, user, "skill.deleted", target_type="skill", target_id=skill_id, target_label=r["title"],
                     name=r["name"])
