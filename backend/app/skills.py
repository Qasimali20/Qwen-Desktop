"""Skills: expert playbooks (frontend, security audit, MLOps, …) that shape how the model works.

Progressive disclosure keeps context small: the system prompt lists only each skill's name and a one-line
"when to use"; the model loads a playbook's full instructions with the use_skill tool when a request
matches. A user can also pick a skill in the composer, which attaches the playbook to that message.

Sources, in precedence order for a name: built-in skills shipped in app/skills/*.md (read-only), team
skills (owner NULL, managed by admins) and the user's personal skills. Names are unique across all three.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from .db import Database
from .tools import Tool, ToolContext, ToolError, ToolResult

log = logging.getLogger(__name__)

NAME_RE = r"^[a-z0-9][a-z0-9-]{0,39}$"
MAX_BODY = 12_000            # chars: a loaded playbook must fit a stored tool result (chat.STORED_TOOL_RESULT)
MAX_DESCRIPTION = 300
BUILTIN_DIR = Path(__file__).with_name("skills")
BUILTIN_PREFIX = "builtin:"
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    title: str
    description: str
    body: str
    scope: str                 # builtin | team | personal

    def public(self, *, editable: bool, with_body: bool = True) -> dict:
        d = {"id": self.id, "name": self.name, "title": self.title, "description": self.description,
             "scope": self.scope, "editable": editable}
        return {**d, "body": self.body} if with_body else d

    def snapshot(self) -> dict:
        """Stored on a message that used the skill, so later edits never rewrite past conversations."""
        return {"id": self.id, "name": self.name, "title": self.title, "body": self.body}


def parse_skill_file(path: Path) -> Skill:
    m = _FRONTMATTER.match(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
    if not m:
        raise ValueError(f"{path.name}: missing --- frontmatter ---")
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    name = meta.get("name") or path.stem
    if not re.match(NAME_RE, name) or not meta.get("title") or not meta.get("description"):
        raise ValueError(f"{path.name}: needs a valid name, a title and a description")
    body = m.group(2).strip()
    if len(body) > MAX_BODY:
        raise ValueError(f"{path.name}: body is {len(body)} chars, over the {MAX_BODY} limit")
    return Skill(BUILTIN_PREFIX + name, name, meta["title"], meta["description"][:MAX_DESCRIPTION], body, "builtin")


@cache
def builtin_skills() -> tuple[Skill, ...]:
    out = []
    for p in sorted(BUILTIN_DIR.glob("*.md")):
        try:
            out.append(parse_skill_file(p))
        except (OSError, ValueError):
            log.exception("skipping built-in skill %s", p.name)
    return tuple(out)


def _from_row(r: dict) -> Skill:
    return Skill(r["id"], r["name"], r["title"], r["description"], r["body"],
                 "team" if r["owner_id"] is None else "personal")


class SkillService:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def builtin_names() -> set[str]:
        return {s.name for s in builtin_skills()}

    def for_user(self, uid: str) -> list[Skill]:
        """Everything the user can use, built-ins first, then by name."""
        builtin = list(builtin_skills())
        taken = {s.name for s in builtin}
        stored = [_from_row(r) for r in self.db.list_skills(uid) if r["name"] not in taken]
        return builtin + sorted(stored, key=lambda s: s.name)

    def get(self, uid: str, skill_id: str) -> Skill | None:
        if skill_id.startswith(BUILTIN_PREFIX):
            return next((s for s in builtin_skills() if s.id == skill_id), None)
        r = self.db.get_skill(skill_id, uid)
        return _from_row(r) if r else None

    def by_name(self, uid: str, name: str) -> Skill | None:
        return next((s for s in self.for_user(uid) if s.name == name), None)

    # ---------------------------------------------------------------- model integration
    def catalogue(self, uid: str) -> str:
        """The <skills> section of the system prompt (empty when there are none)."""
        skills = self.for_user(uid)
        if not skills:
            return ""
        lines = "\n".join(f"- {s.name}: {s.title}. {s.description}" for s in skills)
        return SKILLS_NOTE.format(skills=lines)

    def loader(self, uid: str):
        """use_skill's backend for one user: name -> the playbook as a tool result."""
        def load(name: str) -> ToolResult:
            name = (name or "").strip().lower().removeprefix("/")
            skill = self.by_name(uid, name)
            if not skill:
                known = ", ".join(s.name for s in self.for_user(uid))
                raise ToolError(f"no skill named {name!r}. Available: {known}")
            return ToolResult(f'<skill name="{skill.name}" title="{skill.title}">\n{skill.body}\n</skill>\n'
                              "Follow this playbook for the current request.",
                              {"skill": {"name": skill.name, "title": skill.title}})
        return load


SKILLS_NOTE = """<skills>
Skills are expert playbooks. When the user's request clearly falls in one of these areas, call use_skill with
its name BEFORE you start the work, then follow the playbook. Load only skills that fit (usually one, at most
two); don't load a skill for small talk or quick factual questions, and don't load one that is already in this
conversation.
{skills}
</skills>"""

USER_SKILL_NOTE = ('<skill name="{name}" title="{title}">\nThe user selected this skill for the message below: '
                   'follow the playbook.\n\n{body}\n</skill>')


async def _use_skill(ctx: ToolContext, args: dict) -> ToolResult:
    if not ctx.skills:
        raise ToolError("skills are not available in this chat")
    return ctx.skills(str(args.get("name", "")))


USE_SKILL = Tool(
    "use_skill",
    "Load an expert playbook (skill) by name, from the <skills> list in your instructions. Returns the "
    "instructions to follow for the current request.",
    {"type": "object", "properties": {"name": {"type": "string", "description": "The skill name, e.g. security-audit"}},
     "required": ["name"]},
    _use_skill, "Loading skill")
