"""Process-wide services, created once in the app lifespan."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from .auth import LoginThrottle
from .chat import ChatService
from .connectors import ConnectorService
from .config import Settings
from .db import Database
from .documents import DocumentExtractor
from .gateway import Gateway
from .knowledge import KnowledgeBase
from .llm import LLMClient
from .repos import RepoService
from .sandbox import Sandbox
from .secretbox import SecretBox
from .skills import SkillService
from .workspaces import WorkspaceService

HEALTH_TTL = 10.0


class State:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.data_dir / "qwen-desktop.db", box=SecretBox.from_file())
        self.uploads = settings.data_dir / "uploads"
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.llm = LLMClient(settings)
        self.extractor = DocumentExtractor(self.llm.http, settings.ocr)
        self.kb = KnowledgeBase(self.db, self.llm.http, settings.embedding)
        self.repos = RepoService(self.db, self.kb, settings.data_dir / "repos",
                                 auto_sync_s=settings.repo_sync.interval_minutes * 60)
        self.sandbox = Sandbox(self.db, settings.data_dir / "sandbox", self.uploads)
        self.workspaces = WorkspaceService(self.db, self.sandbox, settings.data_dir / "workspaces")
        self.skills = SkillService(self.db)
        self.connectors = ConnectorService(self.db)
        self.chat = ChatService(settings, self.db, self.llm, self.uploads, self.kb, self.sandbox, self.workspaces,
                                self.skills, self.connectors)
        self.gateway = Gateway(settings, self.db, self.llm)
        self.throttle = LoginThrottle()
        self._health: tuple[float, dict] | None = None
        self._health_lock = asyncio.Lock()

    async def health(self) -> dict:
        async with self._health_lock:
            if not self._health or time.monotonic() - self._health[0] > HEALTH_TTL:
                self._health = (time.monotonic(), await self.llm.health())
            return self._health[1]

    @staticmethod
    def unlink(paths: list[str]) -> None:
        for p in paths:
            Path(p).unlink(missing_ok=True)

    # -- ownership helpers: 404 (not 403) so ids of other users' data are not confirmed
    def conversation(self, cid: str, user: dict) -> dict:
        if not (c := self.db.get_conversation(cid, user["id"])):
            raise HTTPException(404, "Conversation not found")
        return c

    def project(self, pid: str, user: dict, *, edit: bool = False, manage: bool = False) -> dict:
        """The project as visible to `user`, with a `permissions` dict.

        edit:   change instructions, description and knowledge files (owner, admins, or anyone when the
                project is shared with team edit access).
        manage: rename, change sharing, delete (owner; admins too once the project is shared)."""
        if not (p := self.db.get_project(pid, user["id"])):
            raise HTTPException(404, "Project not found")
        p["permissions"] = perms = project_permissions(p, user)
        if (manage and not perms["manage"]) or (edit and not perms["edit"]):
            raise HTTPException(403, "You don't have permission to change this project")
        return p

    def readable_attachment(self, aid: str, user: dict, with_text: bool = False) -> dict:
        """An attachment the user owns, a file in a project they can see (e.g. a team project), or a file shown
        in a chat shared by link (links are open to every signed-in user; attachment ids are unguessable)."""
        att = self.db.get_attachment(aid, with_text=with_text)
        if att and (att["user_id"] == user["id"]
                    or (att["project_id"] and self.db.get_project(att["project_id"], user["id"]))
                    or self.db.shared_attachment(aid)):
            return att
        raise HTTPException(404, "File not found")

    def own_attachments(self, ids: list[str], user: dict) -> None:
        if ids and len(self.db.get_attachments(ids, user["id"])) != len(set(ids)):
            raise HTTPException(404, "Attachment not found")


def project_permissions(p: dict, user: dict) -> dict[str, bool]:
    owner = p["user_id"] == user["id"]
    manage = owner or (user["role"] == "admin" and p["team_access"] != "none")
    return {"owner": owner, "manage": manage, "edit": manage or p["team_access"] == "edit"}


def get_state(request: Request) -> State:
    return request.app.state.s


S = Annotated[State, Depends(get_state)]


def public_attachment(att: dict) -> dict:
    return {k: v for k, v in att.items() if k not in ("path", "text", "user_id")}
