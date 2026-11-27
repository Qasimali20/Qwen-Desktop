"""Activity log: who did what, when, from where — sign-ins, admin actions and team-wide changes.

Entries never contain secrets (passwords, tokens, header values); only what changed and the names involved.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import Request

from .db import Database

log = logging.getLogger("qwen.audit")

RETENTION_DAYS = 365


def client_ip(request: Request | None) -> str | None:
    return request.client.host if request is not None and request.client else None


def record(db: Database, request: Request | None, actor: dict | None, action: str, *,
           target_type: str | None = None, target_id: str | None = None, target_label: str | None = None,
           actor_name: str | None = None, **details: Any) -> None:
    """Write one entry. Never raises: a logging failure must not fail the action being logged."""
    try:
        db.add_audit({
            "actor_id": actor["id"] if actor else None,
            "actor_name": actor_name or (actor["username"] if actor else "unknown"),
            "action": action, "target_type": target_type, "target_id": target_id, "target_label": target_label,
            "details": {k: v for k, v in details.items() if v is not None}, "ip": client_ip(request),
        })
    except Exception:  # noqa: BLE001
        log.exception("could not record %s", action)


def prune(db: Database) -> None:
    n = db.prune_audit(time.time() - RETENTION_DAYS * 86_400)
    if n:
        log.info("pruned %d activity entries older than %d days", n, RETENTION_DAYS)
