"""Chat orchestration: prompt assembly, context budgeting, vision routing and the agent loop.

The stream emits small JSON events consumed by the frontend:
  user_message {message}            persisted user turn
  assistant_start {id, model_id}
  block_start {index, block}        block = {type: thinking|text|tool, ...}
  delta {index, text}               append to a thinking/text block
  tool_result {index, result, ok}
  status {text}                     transient progress (e.g. image analysis)
  title {title}
  done {message}
  error {message}
"""
from __future__ import annotations

import asyncio
import logging
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ChatModel, Settings
from .connectors import ConnectorService
from .db import Database, new_id
from .documents import data_url
from .llm import LLMClient, LLMError
from .knowledge import KnowledgeBase
from .prompts import expand
from .sandbox import Sandbox
from .skills import USE_SKILL, USER_SKILL_NOTE, SkillService
from .repos import ProjectCode
from .tools import (ANALYZE_IMAGE, FETCH_URL, GREP_CODE, LIST_FILES, READ_FILE, RUN_PYTHON, RUN_SHELL, SEARCH_KNOWLEDGE,
                    WEB_SEARCH, WORKSPACE_TOOLS, SearchConfig, ToolContext, ToolResult, run_tool)
from .workspaces import WorkspaceService

log = logging.getLogger("qwen.chat")

MAX_TOOL_ROUNDS = 12
CODE_TOOL_ROUNDS = 20         # exploring a codebase takes more reads/greps
WORKSPACE_TOOL_ROUNDS = 50    # explore → edit → run tests → fix takes many steps
TOOL_OVERHEAD_TOKENS = 1200
IMAGE_TOKENS = 1600          # rough cost of one image in Qwen3-VL at our max_pixels
VISION_IMAGE_LIMIT = 2       # matches --limit-mm-per-prompt on the VL server
STORED_TOOL_RESULT = 12_000  # chars of each tool result kept in history
INLINE_DOCS_SHARE = 0.35     # project docs are inlined while they fit in this share of the context
AUTO_RETRIEVE_K = 8

BASE_PROMPT = """You are Qwen, a helpful, knowledgeable and precise AI assistant running in Qwen Desktop.
Current date and time: {now}.

Formatting:
- Use GitHub-flavoured Markdown. Use headings, lists and tables only when they aid clarity; prefer prose for conversational replies.
- Math: use $inline$ and $$display$$ LaTeX.
- Code: always use fenced code blocks with a language tag. When a block is a complete file, put its path after the
  language (```python app/main.py) so the user can download it with the right name. Show changes to existing files
  as unified diffs in ```diff blocks with ---/+++ file headers, so they can be applied with git apply.

Artifacts:
- When asked to build something substantial and self-contained — a web page, interactive demo, game, chart, SVG graphic, diagram, or a long standalone document — output it as ONE complete fenced code block. The app renders these live in a side panel:
  - ```html  a complete, self-contained HTML document (inline CSS/JS; external libraries only from cdnjs.cloudflare.com, cdn.jsdelivr.net or unpkg.com).
  - ```svg   a standalone SVG.
  - ```mermaid  a Mermaid diagram.
  - ```markdown  a long-form document.
- Keep any explanation outside the block brief.{files}{tools}"""

TOOLS_NOTE = """

Tools:
- You can call tools. Use web_search for anything time-sensitive or that you are unsure about, then fetch_url to read the best sources. Cite sources as Markdown links.
- Never invent tool results."""

KB_NOTE = """<project_knowledge>
This project has a knowledge base of {n} document(s), too large to include in full: {names}.
Passages relevant to the user's latest message have been retrieved with search_knowledge.
- Base your answer on the retrieved passages and cite them inline as [filename, p. N].
- {more}
- If the knowledge base does not contain the answer, say so plainly instead of guessing.
</project_knowledge>"""

CODE_NOTE = """<code_repositories>
This project includes source code, indexed for search. Paths are "<repo>/<path>".
{repos}
- {tools}
- Ground every statement about the code in code you have actually seen; never guess file contents, names or APIs.
- Cite code as `repo/path:line` (or a line range).
- When proposing changes, prefer a unified diff against the real file contents (```diff with --- a/<path> and
  +++ b/<path> headers, paths relative to the repository root, correct @@ line numbers from read_file), or show
  complete edited functions. Mention other places that need updating (callers, tests, config).
</code_repositories>"""

CODE_TOOLS_HINT = ("Explore with list_files, find definitions and usages with grep_code, find code by meaning with "
                   "search_knowledge, and read the relevant files with read_file before answering.")
CODE_NO_TOOLS_HINT = "Only the retrieved code passages below are available to you; say so if they are not enough."

ANALYSIS_NOTE = """
- run_python executes code in a sandbox. Use it for ANY calculation on data, statistics, data cleaning, and charts —
  never do arithmetic on data in your head. The user's attached files are at ./data/<filename>.
  Inspect data first (shape, columns, dtypes, head) before analysing. Make charts clear: titles, axis labels, units.
  After running, explain the findings in plain language; the user sees charts and files automatically.
- run_shell runs bash in the same sandbox: use it to run and test JavaScript/TypeScript (node, tsx, tsc), shell
  scripts and command-line tools. When you write a program for the user, run it to check it works before
  presenting it. There is no internet access (no npm/pip install)."""

FILES_NOTE = """
- When the user asks for a document FILE — Word, PowerPoint, PDF or Excel — create it with run_python instead of
  a markdown block, and save it in the current folder with a descriptive name (e.g. ./quarterly_report.docx);
  the user gets it as a download automatically. Libraries: python-docx (Word), python-pptx (PowerPoint),
  reportlab (PDF; register /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf and DejaVuSans-Bold.ttf for text
  beyond Latin-1), openpyxl (Excel, with formulas and formatting), matplotlib for charts to embed (save PNG
  first). Make them look professional: title, headings, consistent fonts, tables with header rows, page numbers
  in PDFs. In reportlab use SimpleDocTemplate with Platypus flowables (Paragraph, Table, Spacer) and give every
  Table colWidths that add up to at most doc.width, so nothing runs off the page; wrap long cell text in
  Paragraphs. In python-pptx use the title layouts so each slide has a real title. Write the full content — never placeholders. Afterwards, summarise what the file contains in a few
  lines; do not repeat its whole content in the chat."""

WORKSPACE_NOTE = """<workspace name="{name}">
You are working directly in a workspace: a writable copy of the project's code on the server ({origin}).
Your file changes are applied immediately. The user reviews them as a diff, then downloads them or commits and
pushes them to a branch. Paths are relative to the workspace root.
Top-level layout:
{tree}

How to work:
- Explore before changing: list_files, grep and read_file. Always read a file before editing it.
- Change existing files with edit_file (exact string replacement) and create new files with write_file. Do the work
  with these tools — do not paste whole files or diffs into the chat instead.
- Use run_shell to run the code, tests, type checks, linters and builds (e.g. `pytest -q`, `npx tsc --noEmit`,
  `node script.js`, `python3 main.py`). It runs in a sandbox with Python 3.12, Node.js 22, TypeScript, git and
  common tools, and NO internet: packages that are not already in the workspace cannot be installed — say so
  instead of retrying. git is read-only there (status, diff, log work); never commit — the user does that.
- Keep changes focused on the request, follow the existing style, and don't reformat unrelated code.
- When you finish, summarise what you changed (files and why) and how you checked it.{kb}
</workspace>"""

WORKSPACE_KB_HINT = """
- search_knowledge finds code by meaning in the project's indexed repositories. Its paths look like
  "<repo>/<path>": in this workspace, drop the "{repo}/" prefix. The index reflects the original commit, not your
  edits."""

IMAGE_NOTE = ("\n- The user's images are described to you by a vision model. When you need details beyond the "
              "description, call analyze_image with the image_id.")

DESCRIBE_PROMPT = ("Describe this image thoroughly for someone who cannot see it. Transcribe all visible text "
                   "verbatim. For charts and tables, give the data. For UIs, describe layout and content. "
                   "For photos, describe subjects, setting and notable details.")


def estimate_tokens(text: str) -> int:
    return len(text) // 3 + 1


def user_text(msg: dict) -> str:
    """The text the model sees for a user message: the shortcut template expanded with the user's input."""
    prompt = (msg.get("meta") or {}).get("prompt")
    return expand(prompt["body"], msg["content"]) if prompt else msg["content"]


@dataclass
class ProjectContext:
    project: dict | None = None
    docs: list[dict] = field(default_factory=list)
    doc_retrieval: bool = False     # documents too large to inline → searched instead
    repos: list[dict] = field(default_factory=list)   # repositories with at least some indexed files

    @property
    def search_scope(self) -> str | None:
        """What search_knowledge covers in this chat (None: no knowledge search)."""
        if self.repos:
            return "all" if self.doc_retrieval else "code"
        return "docs" if self.doc_retrieval else None


@dataclass
class ChatOptions:
    model_id: str | None = None
    thinking: bool = False
    web_search: bool = False
    analysis: bool = True
    connectors: list[str] = field(default_factory=list)   # MCP server ids the user switched on


def sse(event: str, **data: Any) -> bytes:
    return f"data: {json.dumps({'type': event, **data}, ensure_ascii=False)}\n\n".encode()


class ChatService:
    def __init__(self, settings: Settings, db: Database, llm: LLMClient, uploads: Path, kb: KnowledgeBase,
                 sandbox: Sandbox, workspaces: WorkspaceService, skills: SkillService, connectors: ConnectorService):
        self.settings = settings
        self.db = db
        self.llm = llm
        self.uploads = uploads
        self.kb = kb
        self.sandbox = sandbox
        self.workspaces = workspaces
        self.skills = skills
        self.connectors = connectors
        self._describe_locks: dict[str, asyncio.Lock] = {}

    # ---------------------------------------------------------------- vision
    @property
    def vision(self) -> ChatModel | None:
        vm = self.settings.vision_model
        return self.settings.model(vm) if vm else None

    async def ask_vision(self, url: str, question: str) -> str:
        if not self.vision:
            raise LLMError("No vision model configured")
        msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}},
                                             {"type": "text", "text": question}]}]
        return await self.llm.complete(self.vision, msgs, max_tokens=1500, temperature=0.2)

    async def describe(self, att: dict) -> str:
        """Cached, de-duplicated image description."""
        if att.get("text"):
            return att["text"]
        lock = self._describe_locks.setdefault(att["id"], asyncio.Lock())
        async with lock:
            fresh = self.db.get_attachment(att["id"])
            if fresh and fresh.get("text"):
                return fresh["text"]
            text = await self.ask_vision(data_url(Path(att["path"]), att["mime"]), DESCRIBE_PROMPT)
            self.db.update_attachment(att["id"], {"text": text})
            att["text"] = text
        self._describe_locks.pop(att["id"], None)
        return text

    # ---------------------------------------------------------------- prompt
    def _project_context(self, conv: dict, model: ChatModel) -> ProjectContext:
        """Documents are inlined while they fit comfortably in the model's context; larger knowledge bases
        and code repositories are searched instead."""
        # Access is re-checked on every turn: a team project made private stops feeding members' chats.
        if not conv.get("project_id") or not (project := self.db.get_project(conv["project_id"], conv["user_id"])):
            return ProjectContext()
        docs = [f for f in self.db.project_files(project["id"], with_text=True)
                if f["kind"] == "document" and f.get("text")]
        total = sum(len(d["text"]) for d in docs)
        repos = [r for r in self.db.list_repos(project["id"]) if (r["meta"] or {}).get("indexed")] \
            if self.kb.enabled else []
        return ProjectContext(project, docs, self.kb.enabled and total > model.context * 3 * INLINE_DOCS_SHARE, repos)

    @staticmethod
    def _repo_line(r: dict) -> str:
        m = r["meta"] or {}
        origin = (f"git {r['url']}" + (f" @ {r['ref']}" if r.get("ref") else "")
                  + (f" (commit {r['commit_sha'][:10]})" if r.get("commit_sha") else "")) if r["source"] == "git" \
            else "uploaded archive"
        status = "" if r["status"] == "ready" else f" — still indexing ({m.get('indexed', 0)}/{m.get('files', '?')} files searchable)"
        head = (f"<repo name=\"{r['name']}\" files=\"{m.get('files', '?')}\" languages=\"{', '.join(m.get('langs') or [])}\">"
                f"\nSource: {origin}{status}")
        tree = m.get("tree")
        return head + (f"\nLayout:\n{tree}" if tree else "") + "\n</repo>"

    def workspace_of(self, conv: dict) -> dict | None:
        """The ready workspace this chat works in (owner and project re-checked every turn)."""
        if not (wid := conv.get("workspace_id")):
            return None
        ws = self.db.get_workspace(wid)
        if not ws or ws["status"] != "ready" or ws["user_id"] != conv["user_id"] or ws["project_id"] != conv.get("project_id"):
            return None
        return ws

    def _workspace_note(self, ws: dict, pc: ProjectContext, tools_on: bool) -> str:
        repo = self.db.get_repo(ws["repo_id"]) if ws.get("repo_id") else None
        if repo and repo["source"] == "git":
            origin = f"git clone of {repo['url']} @ {ws['base_ref']}, commit {(ws['base_sha'] or '')[:10]}"
        elif repo:
            origin = f"files of the uploaded archive {repo['name']}"
        else:
            origin = "started empty"
        try:
            tree = self.workspaces.fs(ws["id"]).list_files("", 1)
        except Exception:  # noqa: BLE001 — a missing tree must not break the chat
            tree = "(unavailable)"
        kb = WORKSPACE_KB_HINT.format(repo=repo["name"]) if repo and pc.search_scope and tools_on else ""
        return WORKSPACE_NOTE.format(name=ws["name"], origin=origin, tree=tree, kb=kb)

    def _system_prompt(self, conv: dict, model: ChatModel, tools_on: bool, has_images: bool,
                       pc: ProjectContext | None = None, analysis: bool = False, ws: dict | None = None) -> str:
        pc = pc or ProjectContext()
        tools = ""
        if tools_on:
            tools = (TOOLS_NOTE + (IMAGE_NOTE if has_images and not model.vision else "")
                     + (ANALYSIS_NOTE if analysis else ""))
        parts = [BASE_PROMPT.format(now=datetime.now().astimezone().strftime("%A, %d %B %Y %H:%M %Z"),
                                    files=FILES_NOTE if tools_on and analysis else "", tools=tools)]
        user = self.db.get_settings(conv["user_id"])
        if (ci := (user.get("custom_instructions") or "").strip()):
            parts.append(f"<user_preferences>\n{ci}\n</user_preferences>")
        if (project := pc.project):
            parts.append(f"<project name=\"{project['name']}\">")
            if project["instructions"].strip():
                parts.append(f"<project_instructions>\n{project['instructions'].strip()}\n</project_instructions>")
            if pc.doc_retrieval:
                names = ", ".join(d["filename"] for d in pc.docs[:40])
                more = ("Call search_knowledge again with different wording if the passages do not cover the question."
                        if tools_on else "Only the retrieved passages are available to you.")
                parts.append(KB_NOTE.format(n=len(pc.docs), names=names, more=more))
            else:
                for f in pc.docs:
                    parts.append(f"<document name=\"{f['filename']}\">\n{f['text']}\n</document>")
            if ws:
                parts.append(self._workspace_note(ws, pc, tools_on))
            elif pc.repos:
                parts.append(CODE_NOTE.format(repos="\n".join(self._repo_line(r) for r in pc.repos),
                                              tools=CODE_TOOLS_HINT if tools_on else CODE_NO_TOOLS_HINT))
            parts.append("</project>")
        if conv.get("system_prompt", "").strip():
            parts.append(conv["system_prompt"].strip())
        return "\n\n".join(parts)

    async def _user_content(self, msg: dict, atts: dict[str, dict], model: ChatModel,
                            inline_images: set[str]) -> tuple[Any, int]:
        """Returns (OpenAI content, estimated tokens)."""
        text_parts: list[str] = []
        image_parts: list[dict] = []
        for aid in msg["attachment_ids"]:
            a = atts.get(aid)
            if not a:
                continue
            if a["kind"] == "document":
                path = f' path="data/{a["filename"]}"' if (a.get("meta") or {}).get("data_file") else ""
                text_parts.append(f"<document name=\"{a['filename']}\"{path}>\n{a.get('text') or ''}\n</document>")
            elif aid in inline_images:
                image_parts.append({"type": "image_url",
                                    "image_url": {"url": data_url(Path(a["path"]), a["mime"])}})
            else:
                desc = a.get("text") or "(no description available)"
                text_parts.append(f"<image image_id=\"{aid}\" filename=\"{a['filename']}\">\n"
                                  f"Vision model description:\n{desc}\n</image>")
        if skill := (msg.get("meta") or {}).get("skill"):
            text_parts.append(USER_SKILL_NOTE.format(name=skill["name"], title=skill["title"], body=skill["body"]))
        text_parts.append(user_text(msg))
        text = "\n\n".join(p for p in text_parts if p)
        tokens = estimate_tokens(text) + IMAGE_TOKENS * len(image_parts)
        if image_parts:
            return [*image_parts, {"type": "text", "text": text}], tokens
        return text, tokens

    @staticmethod
    def _assistant_messages(msg: dict, tools_ok: bool = True) -> list[dict]:
        """Rebuild the assistant/tool message sequence from stored blocks. Models without tool
        support get tool activity folded into plain text instead."""
        if not tools_ok:
            text = "\n\n".join(b["text"] for b in msg["blocks"] if b["type"] == "text") or msg["content"]
            return [{"role": "assistant", "content": text or "(no response)"}]
        out: list[dict] = []
        text: list[str] = []
        calls: list[dict] = []
        results: list[dict] = []

        def flush():
            nonlocal text, calls, results
            if calls:
                out.append({"role": "assistant", "content": "".join(text) or None, "tool_calls": calls})
                out.extend(results)
            elif text:
                out.append({"role": "assistant", "content": "".join(text)})
            text, calls, results = [], [], []

        for b in msg["blocks"]:
            if b["type"] == "text":
                if calls:
                    flush()
                text.append(b["text"])
            elif b["type"] == "tool":
                calls.append({"id": b["call_id"], "type": "function",
                              "function": {"name": b["name"], "arguments": b.get("arguments") or "{}"}})
                results.append({"role": "tool", "tool_call_id": b["call_id"],
                                "content": (b.get("result") or "")[:STORED_TOOL_RESULT]})
        flush()
        if not out:
            out.append({"role": "assistant", "content": msg["content"] or "(no response)"})
        return out

    async def build_messages(self, conv: dict, history: list[dict], model: ChatModel, tools_on: bool,
                             emit, analysis: bool = False, ws: dict | None = None, extra_system: str = "",
                             extra_tool_tokens: int = 0,
                             ) -> tuple[list[dict], dict[str, tuple[str, str]], ProjectContext]:
        """Assemble the prompt within the model's context budget.

        Returns (messages, images-for-tools, project context).
        `emit` streams progress events.
        """
        all_ids = [aid for m in history for aid in m["attachment_ids"]]
        atts = self.db.get_attachments(all_ids)
        images = [atts[a] for a in all_ids if a in atts and atts[a]["kind"] == "image"]

        inline: set[str] = set()
        if model.vision:
            inline = {a["id"] for a in images[-VISION_IMAGE_LIMIT:]}
        pending = [a for a in images if a["id"] not in inline and not a.get("text")]
        if pending and self.vision:
            await emit(sse("status", text=f"Analysing {len(pending)} image{'s' * (len(pending) > 1)} with Qwen3-VL…"))
            results = await asyncio.gather(*(self.describe(a) for a in pending), return_exceptions=True)
            for a, r in zip(pending, results):
                if isinstance(r, Exception):
                    a["text"] = f"(vision model unavailable: {r})"

        pc = self._project_context(conv, model)
        system = self._system_prompt(conv, model, tools_on, bool(images), pc, analysis, ws)
        if extra_system:
            system += "\n\n" + extra_system
        budget = (model.context - model.max_tokens - estimate_tokens(system)
                  - (TOOL_OVERHEAD_TOKENS + extra_tool_tokens if tools_on else 0))

        # Walk newest → oldest, keep whole turns while they fit. The latest turn is always kept.
        chunks: list[list[dict]] = []
        used = 0
        for i, m in enumerate(reversed(history)):
            if m["role"] == "user":
                content, cost = await self._user_content(m, atts, model, inline)
                part = [{"role": "user", "content": content}]
            else:
                part = self._assistant_messages(m, tools_on)
                cost = sum(estimate_tokens(json.dumps(p, ensure_ascii=False)) for p in part)
            if i > 0 and used + cost > budget:
                break
            chunks.append(part)
            used += cost
        msgs = [p for chunk in reversed(chunks) for p in chunk]
        while msgs and msgs[0]["role"] != "user":  # a trimmed history must start with a user turn
            msgs.pop(0)

        tool_images = {a["id"]: (a["filename"], data_url(Path(a["path"]), a["mime"])) for a in images}
        return [{"role": "system", "content": system}, *msgs], tool_images, pc

    def _sandbox_inputs(self, conv: dict, history: list[dict]) -> list[dict]:
        """Files exposed to run_python: everything attached in this chat plus the project's files."""
        ids = [aid for m in history for aid in m["attachment_ids"]]
        files = list(self.db.get_attachments(ids).values())
        if conv.get("project_id"):
            files += self.db.project_files(conv["project_id"])
        return files

    # ---------------------------------------------------------------- knowledge
    def knowledge_searcher(self, pid: str, scope: str):
        async def search(query: str, k: int = AUTO_RETRIEVE_K) -> ToolResult:
            hits = await self.kb.search(pid, query, k, scope)
            return ToolResult(self.kb.format_hits(hits), {"sources": self.kb.sources(hits)})
        return search

    # ---------------------------------------------------------------- titles
    async def make_title(self, first_message: str) -> str | None:
        """Short title from the first message: the small utility model first, the default model as fallback."""
        prompt = [
            {"role": "system", "content": "You write very short titles for chat conversations."},
            {"role": "user", "content": "Write a concise 2-6 word title for a conversation that starts "
                                        f"with this message. Reply with the title only.\n\n{first_message[:2000]}"},
        ]
        candidates = dict.fromkeys(m.id for m in (self.settings.model(self.settings.utility_model),
                                                  self.settings.default_model))
        for model_id in candidates:
            try:
                raw = await self.llm.complete(self.settings.model(model_id), prompt, max_tokens=24, temperature=0.3)
            except LLMError:
                log.warning("title generation with %s failed", model_id, exc_info=True)
                continue
            title = raw.strip().strip('"\'#*').splitlines()[0].strip() if raw.strip() else ""
            if title:
                return title[:80]
        return None

    # ---------------------------------------------------------------- main loop
    async def respond(self, conv: dict, opts: ChatOptions) -> AsyncIterator[bytes]:
        model = self.settings.model(opts.model_id or conv.get("model_id"))
        history = self.db.messages(conv["id"])
        tools_on = model.tools
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        title_task = None
        if conv["title"] == "New chat" and sum(m["role"] == "user" for m in history) == 1:
            title_task = asyncio.create_task(self.make_title(user_text(history[-1]) or "Attached files"))

        mid = new_id()
        blocks: list[dict] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        started = time.monotonic()
        saved = False

        def save(error: str | None = None) -> dict:
            nonlocal saved
            saved = True
            content = "\n\n".join(b["text"] for b in blocks if b["type"] == "text").strip()
            u = {**usage, "duration_s": round(time.monotonic() - started, 2)}
            if error:
                u["error"] = error
            return self.db.add_message(conv["id"], "assistant", content, blocks=blocks, model_id=model.id,
                                       usage=u, mid=mid)

        async def emit(chunk: bytes) -> None:
            await queue.put(chunk)

        async def run() -> None:
            try:
                await emit(sse("assistant_start", id=mid, model_id=model.id))
                workspace = self.workspace_of(conv) if tools_on else None
                analysis = tools_on and opts.analysis and self.sandbox.available and not workspace
                # Skills (catalogue + use_skill) and the user's connected MCP servers, when the model has tools.
                extra_tools: dict = {}
                extra_system = []
                if tools_on:
                    if catalogue := self.skills.catalogue(conv["user_id"]):
                        extra_system.append(catalogue)
                        extra_tools[USE_SKILL.name] = USE_SKILL
                    if opts.connectors:
                        mcp_tools, note = self.connectors.for_chat(opts.connectors)
                        extra_tools |= mcp_tools
                        if note:
                            extra_system.append(note)
                extra_schemas = [t.schema() for t in extra_tools.values()]
                messages, images, pc = await self.build_messages(
                    conv, history, model, tools_on, emit, analysis, workspace, extra_system="\n\n".join(extra_system),
                    extra_tool_tokens=estimate_tokens(json.dumps(extra_schemas)) if extra_schemas else 0)
                scope = pc.search_scope
                searcher = self.knowledge_searcher(pc.project["id"], scope) if pc.project and scope else None
                code = ProjectCode(self.db, pc.project["id"]) if pc.repos and not workspace else None
                runner = None
                if workspace:
                    inputs = await asyncio.to_thread(self.sandbox.prepare_inputs, conv["id"],
                                                     self._sandbox_inputs(conv, history))
                    runner = self.workspaces.runner(workspace["id"], inputs)
                elif analysis:
                    runner = self.sandbox.runner(conv, self._sandbox_inputs(conv, history))
                tools = None
                if tools_on:
                    tools = [ANALYZE_IMAGE.schema()] if images and not model.vision else []
                    if workspace:
                        tools += [t.schema() for t in WORKSPACE_TOOLS]
                        if self.sandbox.available:
                            tools.append(RUN_SHELL.schema())
                    if searcher:
                        tools.append(SEARCH_KNOWLEDGE.schema())
                    if code:
                        tools += [LIST_FILES.schema(), GREP_CODE.schema(), READ_FILE.schema()]
                    if analysis:
                        tools += [RUN_PYTHON.schema(), RUN_SHELL.schema()]
                    if opts.web_search:
                        tools += [WEB_SEARCH.schema(), FETCH_URL.schema()]
                    tools += extra_schemas
                    tools = tools or None
                wsc = self.settings.web_search
                ctx = ToolContext(http=self.llm.http, images=images, ask_vision=self.ask_vision,
                                  search=SearchConfig(wsc.provider, wsc.api_key, wsc.url), search_knowledge=searcher,
                                  sandbox=runner, code=code,
                                  ws=self.workspaces.fs(workspace["id"]) if workspace else None,
                                  skills=self.skills.loader(conv["user_id"]), extra_tools=extra_tools)

                # Retrieval mode: search the knowledge base for the latest question up front, shown to
                # the user as a regular tool step (with sources) and fed to the model as its result.
                query = next((m["content"] or user_text(m) for m in reversed(history) if m["role"] == "user"), "").strip()
                if searcher and query:
                    await emit(sse("status", text="Searching knowledge base…"))
                    res = await searcher(query[:2000])
                    call_id = f"kb-{new_id()[:12]}"
                    block = {"type": "tool", "call_id": call_id, "name": "search_knowledge",
                             "arguments": json.dumps({"query": query[:300]}), "result": res.text[:STORED_TOOL_RESULT],
                             "ok": True, **res.extra}
                    blocks.append(block)
                    await emit(sse("block_start", index=len(blocks) - 1, block=block))
                    if tools_on:
                        messages += [{"role": "assistant", "content": None, "tool_calls": [{
                                         "id": call_id, "type": "function",
                                         "function": {"name": "search_knowledge", "arguments": block["arguments"]}}]},
                                     {"role": "tool", "tool_call_id": call_id, "content": block["result"]}]
                    else:
                        messages[0]["content"] += f"\n\n<retrieved_passages>\n{block['result']}\n</retrieved_passages>"

                max_rounds = WORKSPACE_TOOL_ROUNDS if workspace else CODE_TOOL_ROUNDS if code else MAX_TOOL_ROUNDS
                for _round in range(max_rounds + 1):
                    current: dict | None = None
                    round_text: list[str] = []
                    calls = []
                    async for ev in self.llm.stream(model, messages, tools=tools, thinking=opts.thinking):
                        if ev.kind in ("reasoning", "text"):
                            btype = "thinking" if ev.kind == "reasoning" else "text"
                            if current is None or current["type"] != btype:
                                current = {"type": btype, "text": ""}
                                blocks.append(current)
                                await emit(sse("block_start", index=len(blocks) - 1, block=current))
                            current["text"] += ev.text
                            if btype == "text":
                                round_text.append(ev.text)
                            await emit(sse("delta", index=len(blocks) - 1, text=ev.text))
                        elif ev.kind == "tool_calls":
                            calls = ev.tool_calls or []
                        elif ev.kind == "finish" and ev.usage:
                            usage["prompt_tokens"] += ev.usage.get("prompt_tokens", 0)
                            usage["completion_tokens"] += ev.usage.get("completion_tokens", 0)

                    if not calls or _round == max_rounds:
                        break
                    messages.append({"role": "assistant", "content": "".join(round_text) or None,
                                     "tool_calls": [{"id": c.id or new_id(), "type": "function",
                                                     "function": {"name": c.name, "arguments": c.arguments}}
                                                    for c in calls]})
                    idxs = []
                    for c, tc in zip(calls, messages[-1]["tool_calls"]):
                        block = {"type": "tool", "call_id": tc["id"], "name": c.name, "arguments": c.arguments,
                                 "result": None, "ok": None}
                        blocks.append(block)
                        idxs.append(len(blocks) - 1)
                        await emit(sse("block_start", index=idxs[-1], block=block))

                    async def exec_one(i: int) -> None:
                        b = blocks[i]
                        result, ok, extra = await run_tool(ctx, b["name"], b["arguments"])
                        b["result"], b["ok"] = result[:STORED_TOOL_RESULT], ok
                        b.update(extra)
                        await emit(sse("tool_result", index=i, result=b["result"], ok=ok, **extra))

                    await asyncio.gather(*(exec_one(i) for i in idxs))
                    messages += [{"role": "tool", "tool_call_id": blocks[i]["call_id"], "content": blocks[i]["result"]}
                                 for i in idxs]

                if title_task:
                    try:
                        title = await asyncio.wait_for(title_task, timeout=15)
                    except Exception:  # noqa: BLE001 — a missing title must never fail the reply
                        log.warning("title generation failed", exc_info=True)
                        title = None
                    if title:
                        self.db.update_conversation(conv["id"], {"title": title})
                        await emit(sse("title", title=title))
                await emit(sse("done", message=save()))
            except LLMError as e:
                await emit(sse("error", message=str(e), saved=save(error=str(e)) if blocks else None))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                await emit(sse("error", message=f"{type(e).__name__}: {e}"))
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())
        try:
            while (chunk := await queue.get()) is not None:
                yield chunk
        finally:
            # Client disconnected (stop button) or completed: keep what was generated.
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            if title_task and not title_task.done():
                title_task.cancel()
            if not saved and blocks:
                save(error="stopped")
