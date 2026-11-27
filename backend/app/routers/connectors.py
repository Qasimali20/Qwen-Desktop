"""MCP connectors: admins register remote MCP servers; users switch them on for their chats."""
from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .. import audit
from ..auth import Admin, User
from ..connectors import NAME_RE, ConnectorError, list_tools
from ..state import S

router = APIRouter(tags=["connectors"])

_HEADER_NAME = r"^[A-Za-z0-9-]{1,64}$"


def _check_url(v: str) -> str:
    u = urlparse(v.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("must be an http(s) URL")
    return v.strip()


def _check_headers(v: dict[str, str] | None) -> dict[str, str] | None:
    if v is None:
        return v
    if len(v) > 12:
        raise ValueError("too many headers")
    for k, val in v.items():
        if not re.match(_HEADER_NAME, k) or len(val) > 4096 or "\n" in val or "\r" in val:
            raise ValueError(f"invalid header {k!r}")
    return v


class ConnectorIn(BaseModel):
    name: str = Field(pattern=NAME_RE)
    label: str = Field(min_length=1, max_length=60)
    description: str = Field("", max_length=300)
    url: str = Field(max_length=2000)
    transport: Literal["http", "sse"] = "http"
    headers: dict[str, str] = {}

    _url = field_validator("url")(_check_url)
    _headers = field_validator("headers")(_check_headers)


class ConnectorPatch(BaseModel):
    label: str | None = Field(None, min_length=1, max_length=60)
    description: str | None = Field(None, max_length=300)
    url: str | None = Field(None, max_length=2000)
    transport: Literal["http", "sse"] | None = None
    headers: dict[str, str] | None = None      # replaces all headers when given
    enabled: bool | None = None
    disabled_tools: list[str] | None = None

    _url = field_validator("url")(lambda v: v if v is None else _check_url(v))
    _headers = field_validator("headers")(_check_headers)


def _server(s, sid: str) -> dict:
    if not (srv := s.db.get_mcp_server(sid)):
        raise HTTPException(404, "Connector not found")
    return srv


@router.get("/api/connectors")
def available(_: User, s: S):
    """Servers a user can switch on in the composer."""
    return [s.connectors.public(x) for x in s.connectors.available() if x["tool_list"]]


@router.get("/api/admin/connectors")
def list_all(_: Admin, s: S):
    return [s.connectors.public(x, admin=True) for x in s.db.list_mcp_servers()]


@router.post("/api/admin/connectors", status_code=201)
async def create(body: ConnectorIn, admin: Admin, s: S, request: Request):
    if s.db.mcp_name_taken(body.name):
        raise HTTPException(409, f"A connector named '{body.name}' already exists")
    # Connect first, so a wrong URL, transport or token is reported now instead of in a chat.
    try:
        await list_tools(body.url, body.transport, body.headers)
    except ConnectorError as e:
        raise HTTPException(400, f"Could not connect: {e}") from e
    srv = s.db.create_mcp_server(body.model_dump(), admin["id"])
    srv = await s.connectors.refresh(srv["id"])
    audit.record(s.db, request, admin, "connector.created", target_type="connector", target_id=srv["id"],
                 target_label=srv["label"], url=srv["url"], headers=sorted(body.headers) or None,
                 tools_off=srv["disabled_tools"] or None)
    return s.connectors.public(srv, admin=True)


@router.patch("/api/admin/connectors/{sid}")
async def update(sid: str, body: ConnectorPatch, me: Admin, s: S, request: Request):
    before = srv = _server(s, sid)
    fields = body.model_dump(exclude_none=True)
    if "disabled_tools" in fields:
        known = {t["name"] for t in srv["tool_list"] or []}
        fields["disabled_tools"] = sorted(set(fields["disabled_tools"]) & known)
    if "enabled" in fields:
        fields["enabled"] = int(fields["enabled"])
    srv = s.db.update_mcp_server(sid, fields)
    if {"url", "transport", "headers"} & fields.keys():
        srv = await s.connectors.refresh(sid)
    # What changed, without header values.
    changes = {k: [before[k], srv[k]] for k in ("label", "description", "url", "transport") if k in fields and before[k] != srv[k]}
    if "enabled" in fields and bool(before["enabled"]) != bool(srv["enabled"]):
        changes["enabled"] = [bool(before["enabled"]), bool(srv["enabled"])]
    was_off, now_off = set(before["disabled_tools"] or []), set(srv["disabled_tools"] or [])
    audit.record(s.db, request, me, "connector.updated", target_type="connector", target_id=sid, target_label=srv["label"],
                 changes=changes or None, headers_replaced=sorted(fields["headers"]) if "headers" in fields else None,
                 tools_on=sorted(was_off - now_off) or None, tools_off=sorted(now_off - was_off) or None)
    return s.connectors.public(srv, admin=True)


@router.post("/api/admin/connectors/{sid}/refresh")
async def refresh(sid: str, _: Admin, s: S):
    _server(s, sid)
    return s.connectors.public(await s.connectors.refresh(sid), admin=True)


@router.delete("/api/admin/connectors/{sid}", status_code=204)
def delete(sid: str, me: Admin, s: S, request: Request):
    srv = _server(s, sid)
    s.db.delete_mcp_server(sid)
    audit.record(s.db, request, me, "connector.deleted", target_type="connector", target_id=sid, target_label=srv["label"],
                 url=srv["url"])
