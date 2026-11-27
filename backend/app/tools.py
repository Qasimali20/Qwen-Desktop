"""Server-side tools exposed to tool-capable models."""
from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

MAX_FETCH_BYTES = 3_000_000
MAX_TOOL_TEXT = 24_000
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


@dataclass
class SearchConfig:
    provider: str = "wikipedia"   # brave | tavily | searxng | wikipedia
    api_key: str = ""
    url: str = ""


class ToolError(Exception):
    """An expected failure whose message is shown to the model as is (no exception type prefix)."""


@dataclass
class ToolResult:
    """Text goes to the model; `extra` is merged into the UI block (sources, output files)."""
    text: str
    extra: dict = field(default_factory=dict)


@dataclass
class ToolContext:
    http: httpx.AsyncClient
    search: SearchConfig = field(default_factory=SearchConfig)
    # image attachment id -> (filename, data_url)
    images: dict[str, tuple[str, str]] = field(default_factory=dict)
    ask_vision: Callable[[str, str], Awaitable[str]] | None = None
    # query, k -> ToolResult with sources (project knowledge base)
    search_knowledge: Callable[[str, int], Awaitable[ToolResult]] | None = None
    # (kind "python"|"shell", script, timeout_s) -> ToolResult with outputs: the chat's scratch sandbox or,
    # in a project workspace, the working copy
    sandbox: Callable[[str, str, int], Awaitable[ToolResult]] | None = None
    # the project's code repositories (repos.ProjectCode): read_file / list_files / grep_code
    code: Any = None
    # a writable project workspace (workspaces.WorkspaceFS): when set, the file tools work on it instead
    ws: Any = None
    # skill name -> playbook (skills.SkillService.loader) for use_skill
    skills: Callable[[str], ToolResult] | None = None
    # tools defined per chat (connected MCP servers), looked up after the built-in registry
    extra_tools: dict[str, Tool] = field(default_factory=dict)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[[ToolContext, dict], Awaitable[str | ToolResult]]
    label: str = ""  # short UI verb, e.g. "Searching the web"
    timeout: float = 90

    def schema(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


# ---------------------------------------------------------------- HTML → text
class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe", "template"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
             "pre", "blockquote", "table", "ul", "ol", "dd", "dt"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3") and not self._skip:
            self.parts.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        t = "".join(self.parts)
        t = re.sub(r"[ \t\r\f\v]+", " ", t)
        t = re.sub(r"\n\s*\n\s*\n+", "\n\n", t)
        return t.strip()


def html_to_text(markup: str) -> tuple[str, str]:
    p = _TextExtractor()
    p.feed(markup)
    return p.title.strip(), p.text()


# ---------------------------------------------------------------- SSRF guard
async def _assert_public(url: str) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("Only public http(s) URLs can be fetched")
    infos = await asyncio.get_running_loop().getaddrinfo(u.hostname, u.port or 443, type=socket.SOCK_STREAM)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"Refusing to fetch non-public address {u.hostname}")


# ---------------------------------------------------------------- tools
# ---------------------------------------------------------------- search providers
def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


async def _search_brave(ctx: "ToolContext", q: str, n: int) -> list[dict]:
    r = await ctx.http.get("https://api.search.brave.com/res/v1/web/search", params={"q": q, "count": n},
                           headers={"X-Subscription-Token": ctx.search.api_key, "Accept": "application/json"},
                           timeout=15)
    r.raise_for_status()
    return [{"title": x["title"], "url": x["url"], "snippet": _strip_tags(x.get("description", ""))}
            for x in r.json().get("web", {}).get("results", [])]


async def _search_tavily(ctx: "ToolContext", q: str, n: int) -> list[dict]:
    r = await ctx.http.post("https://api.tavily.com/search", timeout=20,
                            json={"api_key": ctx.search.api_key, "query": q, "max_results": n})
    r.raise_for_status()
    return [{"title": x["title"], "url": x["url"], "snippet": x.get("content", "")} for x in r.json()["results"]]


async def _search_searxng(ctx: "ToolContext", q: str, n: int) -> list[dict]:
    r = await ctx.http.get(f"{ctx.search.url.rstrip('/')}/search", params={"q": q, "format": "json"}, timeout=15)
    r.raise_for_status()
    return [{"title": x.get("title", ""), "url": x["url"], "snippet": x.get("content", "")}
            for x in r.json().get("results", [])]


async def _search_wikipedia(ctx: "ToolContext", q: str, n: int) -> list[dict]:
    # Models often add "wikipedia"/"site:" noise that derails CirrusSearch ranking.
    q = re.sub(r"\bsite:\S+|\bwikipedia\b|\b(definition|meaning) of\b", " ", q, flags=re.I).strip() or q
    r = await ctx.http.get("https://en.wikipedia.org/w/api.php", timeout=15, headers={"User-Agent": "QwenDesktop/1.0"},
                           params={"action": "query", "list": "search", "srsearch": q, "srlimit": n,
                                   "format": "json"})
    r.raise_for_status()
    return [{"title": x["title"], "url": "https://en.wikipedia.org/wiki/" + x["title"].replace(" ", "_"),
             "snippet": _strip_tags(x.get("snippet", ""))} for x in r.json()["query"]["search"]]


SEARCH_PROVIDERS = {"brave": _search_brave, "tavily": _search_tavily, "searxng": _search_searxng,
                    "wikipedia": _search_wikipedia}


async def _web_search(ctx: ToolContext, args: dict) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "Error: empty query"
    n = max(1, min(int(args.get("max_results", 8)), 15))
    provider = SEARCH_PROVIDERS[ctx.search.provider]
    results = (await provider(ctx, query, n))[:n]
    if not results:
        return f"No results found for: {query}"
    note = ("Note: only Wikipedia search is available; use fetch_url on known sites for other sources.\n"
            if ctx.search.provider == "wikipedia" else "")
    return note + json.dumps(results, ensure_ascii=False)


async def _fetch_url(ctx: ToolContext, args: dict) -> str:
    url = str(args.get("url", "")).strip()
    url = url if "://" in url else f"https://{url}"
    for _ in range(5):  # follow redirects manually so every hop is checked
        await _assert_public(url)
        async with ctx.http.stream("GET", url, headers={"User-Agent": _UA, "Accept": "text/html,*/*;q=0.8"},
                                   timeout=20, follow_redirects=False) as resp:
            if resp.is_redirect and resp.headers.get("location"):
                url = urljoin(url, resp.headers["location"])
                continue
            if resp.status_code >= 400:
                return f"Error: HTTP {resp.status_code} fetching {url}"
            ctype = resp.headers.get("content-type", "")
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf += chunk
                if len(buf) > MAX_FETCH_BYTES:
                    break
            break
    else:
        return "Error: too many redirects"
    raw = bytes(buf).decode(resp.encoding or "utf-8", errors="replace")
    if "html" in ctype or raw.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        title, text = html_to_text(raw)
    elif any(t in ctype for t in ("text/", "json", "xml", "javascript")):
        title, text = "", raw
    else:
        return f"Fetched {url} but its content type ({ctype}) is not text."
    truncated = len(text) > MAX_TOOL_TEXT
    text = text[:MAX_TOOL_TEXT]
    head = f"URL: {url}\n" + (f"Title: {title}\n" if title else "")
    return head + "\n" + text + ("\n\n[truncated]" if truncated else "")


async def _analyze_image(ctx: ToolContext, args: dict) -> str:
    image_id = str(args.get("image_id", ""))
    question = str(args.get("question", "Describe this image in detail.")).strip()
    if image_id not in ctx.images:
        known = ", ".join(f"{k} ({v[0]})" for k, v in ctx.images.items()) or "none"
        return f"Error: unknown image_id. Available images: {known}"
    if not ctx.ask_vision:
        return "Error: no vision model configured"
    return await ctx.ask_vision(ctx.images[image_id][1], question)


async def _search_knowledge(ctx: ToolContext, args: dict) -> ToolResult | str:
    if not ctx.search_knowledge:
        return "Error: this conversation has no knowledge base"
    query = str(args.get("query", "")).strip()
    if not query:
        return "Error: empty query"
    return await ctx.search_knowledge(query, max(1, min(int(args.get("k", 8)), 15)))


SHELL_TIMEOUT_S = 120
SHELL_MAX_TIMEOUT_S = 600


async def _run_python(ctx: ToolContext, args: dict) -> ToolResult | str:
    if not ctx.sandbox:
        return "Error: the code sandbox is not available"
    code = str(args.get("code", ""))
    if not code.strip():
        return "Error: empty code"
    return await ctx.sandbox("python", code, SHELL_TIMEOUT_S)


async def _run_shell(ctx: ToolContext, args: dict) -> ToolResult | str:
    if not ctx.sandbox:
        return "Error: the code sandbox is not available"
    command = str(args.get("command", ""))
    if not command.strip():
        return "Error: empty command"
    try:
        timeout = int(args.get("timeout") or SHELL_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout = SHELL_TIMEOUT_S
    return await ctx.sandbox("shell", command, max(5, min(timeout, SHELL_MAX_TIMEOUT_S)))


def _opt_int(args: dict, k: str) -> int | None:
    v = args.get(k)
    return int(v) if v not in (None, "") else None


async def _read_file(ctx: ToolContext, args: dict) -> str:
    target = ctx.ws or ctx.code
    if not target:
        return "Error: this conversation has no code repositories or workspace"
    return await asyncio.to_thread(target.read_file, str(args.get("path", "")), _opt_int(args, "start_line"),
                                   _opt_int(args, "end_line"))


async def _list_files(ctx: ToolContext, args: dict) -> str:
    target = ctx.ws or ctx.code
    if not target:
        return "Error: this conversation has no code repositories or workspace"
    return await asyncio.to_thread(target.list_files, str(args.get("path") or ""), int(args.get("depth") or 2))


async def _write_file(ctx: ToolContext, args: dict) -> ToolResult | str:
    if not ctx.ws:
        return "Error: this conversation has no workspace to write to"
    if "content" not in args:
        return "Error: content is required"
    return await asyncio.to_thread(ctx.ws.write_file, str(args.get("path", "")), str(args["content"]))


async def _edit_file(ctx: ToolContext, args: dict) -> ToolResult | str:
    if not ctx.ws:
        return "Error: this conversation has no workspace to edit"
    return await asyncio.to_thread(ctx.ws.edit_file, str(args.get("path", "")), str(args.get("old_string", "")),
                                   str(args.get("new_string", "")), bool(args.get("replace_all", False)))


async def _grep(ctx: ToolContext, args: dict) -> str:
    if not ctx.ws:
        return "Error: this conversation has no workspace"
    return await asyncio.to_thread(ctx.ws.grep, str(args.get("pattern", "")), args.get("path_glob") or None,
                                   bool(args.get("regex", False)), bool(args.get("ignore_case", True)))


async def _grep_code(ctx: ToolContext, args: dict) -> str:
    if not ctx.code:
        return "Error: this conversation has no code repositories"
    return await asyncio.to_thread(ctx.code.grep, str(args.get("pattern", "")), args.get("path_glob") or None,
                                   bool(args.get("regex", False)), bool(args.get("ignore_case", True)))


SEARCH_KNOWLEDGE = Tool(
    "search_knowledge",
    "Search the project's knowledge base (uploaded documents and, if present, code repositories) and return the "
    "most relevant passages with their source (file and page, or path and lines). Combines meaning-based and "
    "keyword search. Use specific wording; search again with different terms if the passages do not answer "
    "the question.",
    {"type": "object", "properties": {
        "query": {"type": "string", "description": "What to look for"},
        "k": {"type": "integer", "description": "Number of passages, 1-15 (default 8)"},
    }, "required": ["query"]},
    _search_knowledge, "Searching knowledge base")

RUN_PYTHON = Tool(
    "run_python",
    "Execute Python 3.12 code in an isolated sandbox for data analysis, statistics, calculations and charts. "
    "Available: pandas, numpy, scipy, statsmodels, scikit-learn, matplotlib, seaborn, openpyxl, pyarrow. "
    "Each call runs in a FRESH process (variables do not persist) but files persist across calls in the "
    "working directory. User files are in ./data/ (read-only). Save charts with plt.savefig('name.png') "
    "or just leave figures open; save tables/exports into ./out/ to make them downloadable. "
    "Print the results you need to see — stdout is returned (truncated if very long). No internet access.",
    {"type": "object", "properties": {"code": {"type": "string", "description": "Python source to run"}},
     "required": ["code"]},
    _run_python, "Running analysis")

RUN_SHELL = Tool(
    "run_shell",
    "Run a bash script in the isolated sandbox (Debian, no internet). Available: Python 3.12 (with the packages "
    "above plus pytest, ruff), Node.js 22 with npm, TypeScript (tsc) and tsx, git, ripgrep (rg), jq, sqlite3, "
    "make, zip/unzip, tree. Use it to run JavaScript/TypeScript, shell pipelines, tests, linters and file "
    "operations. Runs in the same working directory as run_python, so files persist between calls; each call is "
    "a fresh process. stdout and stderr are returned together (truncated if very long). npm install and other "
    "downloads do not work: there is no network.",
    {"type": "object", "properties": {
        "command": {"type": "string", "description": "Bash script to run (multiple lines allowed; runs with pipefail)"},
        "timeout": {"type": "integer", "description": "Seconds before the run is stopped, 5-600 (default 120)"},
    }, "required": ["command"]},
    _run_shell, "Running command")

READ_FILE = Tool(
    "read_file",
    "Read a source file from the project's code repositories, with line numbers. Paths look like "
    "\"<repo>/<path>\" (as shown by list_files, grep_code and search results). Reads up to 300 lines by default; "
    "pass start_line/end_line for other parts of long files.",
    {"type": "object", "properties": {
        "path": {"type": "string", "description": "File path, e.g. myrepo/src/app.py"},
        "start_line": {"type": "integer", "description": "First line (1-based, optional)"},
        "end_line": {"type": "integer", "description": "Last line (optional, at most 800 lines per call)"},
    }, "required": ["path"]},
    _read_file, "Reading file")

LIST_FILES = Tool(
    "list_files",
    "List files and folders in the project's code repositories, with file counts per folder and line counts "
    "per file. Start with no path to see every repository.",
    {"type": "object", "properties": {
        "path": {"type": "string", "description": "Folder to list, e.g. myrepo/src (default: everything)"},
        "depth": {"type": "integer", "description": "Levels to expand below the folder, 1-6 (default 2)"},
    }},
    _list_files, "Listing files")

GREP_CODE = Tool(
    "grep_code",
    "Exact text search across every file in the project's code repositories; returns path:line: text. Best for "
    "finding where a function, class, variable, config key or error message is defined or used.",
    {"type": "object", "properties": {
        "pattern": {"type": "string", "description": "Text to find (a regex when regex=true)"},
        "path_glob": {"type": "string", "description": "Only these files, e.g. *.py or myrepo/src/*; comma-separate several"},
        "regex": {"type": "boolean", "description": "Treat pattern as a Python regular expression (default false)"},
        "ignore_case": {"type": "boolean", "description": "Default true"},
    }, "required": ["pattern"]},
    _grep_code, "Searching code")

# ---- workspace tools: the same names as the repository tools where the meaning is the same, so a model that
# knows read_file/list_files uses them naturally; paths are relative to the workspace root.
WS_READ_FILE = Tool(
    "read_file",
    "Read a file from the workspace, with line numbers (up to 400 lines per call by default; pass start_line/"
    "end_line for other parts of long files). Always read a file before editing it.",
    {"type": "object", "properties": {
        "path": {"type": "string", "description": "Path relative to the workspace root, e.g. src/app.py"},
        "start_line": {"type": "integer", "description": "First line (1-based, optional)"},
        "end_line": {"type": "integer", "description": "Last line (optional, at most 1000 lines per call)"},
    }, "required": ["path"]},
    _read_file, "Reading file")

WS_LIST_FILES = Tool(
    "list_files",
    "List files and folders in the workspace as a tree with sizes. Dependency and build folders (node_modules, "
    ".venv, dist, …) are shown but not expanded.",
    {"type": "object", "properties": {
        "path": {"type": "string", "description": "Folder relative to the workspace root (default: the root)"},
        "depth": {"type": "integer", "description": "Levels to expand, 1-6 (default 2)"},
    }},
    _list_files, "Listing files")

WS_WRITE_FILE = Tool(
    "write_file",
    "Create a new file, or replace an existing file's entire content. Parent folders are created as needed. "
    "Prefer edit_file for changes to existing files. Write the complete content — never placeholders like "
    "'rest of file unchanged'.",
    {"type": "object", "properties": {
        "path": {"type": "string", "description": "Path relative to the workspace root, e.g. src/utils/dates.ts"},
        "content": {"type": "string", "description": "The full file content"},
    }, "required": ["path", "content"]},
    _write_file, "Writing file")

WS_EDIT_FILE = Tool(
    "edit_file",
    "Edit an existing file by exact string replacement: old_string must match the file exactly (including "
    "indentation and line breaks) and be unique, unless replace_all is true. Include enough surrounding lines "
    "to make it unique. Read the file first; never include the line-number prefixes from read_file.",
    {"type": "object", "properties": {
        "path": {"type": "string", "description": "Path relative to the workspace root"},
        "old_string": {"type": "string", "description": "Exact text to replace"},
        "new_string": {"type": "string", "description": "Replacement text"},
        "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
    }, "required": ["path", "old_string", "new_string"]},
    _edit_file, "Editing file")

WS_GREP = Tool(
    "grep",
    "Search file contents across the workspace; returns path:line: text. Use it to find definitions, usages, "
    "config keys and error messages.",
    {"type": "object", "properties": {
        "pattern": {"type": "string", "description": "Text to find (a Python regex when regex=true)"},
        "path_glob": {"type": "string", "description": "Only these files, e.g. *.py or src/*; comma-separate several"},
        "regex": {"type": "boolean", "description": "Treat pattern as a regular expression (default false)"},
        "ignore_case": {"type": "boolean", "description": "Default true"},
    }, "required": ["pattern"]},
    _grep, "Searching files")

WORKSPACE_TOOLS = (WS_LIST_FILES, WS_READ_FILE, WS_GREP, WS_EDIT_FILE, WS_WRITE_FILE)

WEB_SEARCH = Tool(
    "web_search",
    "Search the web for current information. Returns a JSON list of {title, url, snippet}. "
    "Use fetch_url afterwards to read a promising page in full.",
    {"type": "object", "properties": {
        "query": {"type": "string", "description": "Search query"},
        "max_results": {"type": "integer", "description": "1-15, default 8"},
    }, "required": ["query"]},
    _web_search, "Searching the web")

FETCH_URL = Tool(
    "fetch_url",
    "Fetch a public web page or text file and return its readable text content.",
    {"type": "object", "properties": {"url": {"type": "string", "description": "Absolute URL"}},
     "required": ["url"]},
    _fetch_url, "Reading page")

ANALYZE_IMAGE = Tool(
    "analyze_image",
    "Ask the vision model (Qwen3-VL) a specific question about an image the user attached. "
    "Use for details, exact text, counts, charts, or anything not covered by the provided description.",
    {"type": "object", "properties": {
        "image_id": {"type": "string", "description": "The image_id shown next to the attachment"},
        "question": {"type": "string", "description": "What to find out about the image"},
    }, "required": ["image_id", "question"]},
    _analyze_image, "Looking at image")

REGISTRY: dict[str, Tool] = {t.name: t for t in (WEB_SEARCH, FETCH_URL, ANALYZE_IMAGE, SEARCH_KNOWLEDGE,
                                                  RUN_PYTHON, RUN_SHELL, READ_FILE, LIST_FILES, GREP_CODE,
                                                  WS_WRITE_FILE, WS_EDIT_FILE, WS_GREP)}
TOOL_TIMEOUT = {"run_python": SHELL_TIMEOUT_S + 30, "run_shell": SHELL_MAX_TIMEOUT_S + 30}


async def run_tool(ctx: ToolContext, name: str, raw_args: str) -> tuple[str, bool, dict]:
    """Returns (result_text, ok, ui_extra). Never raises: errors are reported to the model."""
    tool = REGISTRY.get(name) or ctx.extra_tools.get(name)
    if not tool:
        return f"Error: unknown tool {name!r}", False, {}
    try:
        args: Any = json.loads(raw_args) if raw_args.strip() else {}
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except ValueError as e:
        return f"Error: invalid arguments: {e}", False, {}
    try:
        out = await asyncio.wait_for(tool.run(ctx, args), timeout=TOOL_TIMEOUT.get(name, tool.timeout))
        res = out if isinstance(out, ToolResult) else ToolResult(out)
        return res.text[:MAX_TOOL_TEXT], not res.text.startswith("Error:"), res.extra
    except ToolError as e:
        return f"Error: {e}", False, {}
    except Exception as e:  # noqa: BLE001 — surfaced to the model, which can recover
        return f"Error: {type(e).__name__}: {e}", False, {}
