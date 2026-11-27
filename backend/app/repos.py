"""Code repositories in projects: fetch (zip upload or shallow git clone), filter, chunk, embed.

Pipeline per repo (a background job, resumable):
  fetching  read the zip / clone the repo → filtered text files → repo_files (diffed by sha, so a
            re-sync keeps the chunks of unchanged files)
  indexing  files whose indexed_sha != sha are chunked at definition boundaries and embedded
  ready     a layout summary (tree, languages) is stored for the system prompt

The http and https servers share one database, so a job runs under a lease row-lock in `repos`;
a sweeper in every process picks up jobs whose lease expired (crash, restart).
"""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

import numpy as np
import pathspec

from . import secretscan
from .db import Database
from .knowledge import KnowledgeBase

log = logging.getLogger("qwen.repos")

# ---------------------------------------------------------------- limits
MAX_FILE_BYTES = 512 * 1024          # larger files are almost always generated or data
MAX_JSON_BYTES = 64 * 1024           # big JSON/YAML is data, not code
MAX_FILES = 15_000
MAX_TOTAL_BYTES = 24 * 1024 * 1024   # ~7 min of embedding at the measured ~25 chunks/s
MAX_ZIP_ENTRIES = 200_000
CLONE_TIMEOUT_S = 300
LEASE_TTL_S = 90.0
SWEEP_EVERY_S = 30.0
JOB_CONCURRENCY = 2                  # per process; embedding concurrency is capped in KnowledgeBase
FILES_PER_BATCH = 48
AUTO_SYNC_TICK_S = 60.0              # how often each process looks for repos due a remote check
AUTO_SYNC_CONCURRENCY = 4            # parallel `git ls-remote` calls per process
LS_REMOTE_TIMEOUT_S = 90             # GitHub can take a minute to answer from this network
AUTO_SYNC_REPORT_AFTER = 3          # consecutive failed checks before the repo card / health page show it

CHUNK_TARGET = 2400                  # chars; code chunks follow definitions, so they vary around this
CHUNK_MIN = 600
CHUNK_MAX = 4000

# ---------------------------------------------------------------- what counts as source
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "bower_components", "jspm_packages", "vendor", "dist", "build",
    ".next", ".nuxt", ".svelte-kit", ".output", ".turbo", ".parcel-cache", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox", ".gradle", ".idea", ".vscode", "coverage",
    "htmlcov", ".terraform", ".cache", "Pods", "DerivedData", ".dart_tool", "site-packages", ".eggs",
    "target", "obj", ".angular", ".expo", "storybook-static", "__snapshots__",
}
SKIP_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "Cargo.lock",
    "composer.lock", "Gemfile.lock", "go.sum", "uv.lock", "bun.lockb", "flake.lock", "mix.lock",
    "packages.lock.json", "gradle.lockfile",
}
SKIP_SUFFIXES = (".min.js", ".min.css", ".map", ".snap", ".pb.go", "_pb2.py", ".g.dart", ".freezed.dart",
                 ".designer.cs", ".bundle.js", ".chunk.js")

LANGS: dict[str, str] = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "jsx", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx", ".mts": "typescript", ".cts": "typescript",
    ".vue": "vue", ".svelte": "svelte", ".astro": "astro", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".groovy": "groovy", ".gradle": "groovy", ".go": "go", ".rs": "rust", ".c": "c",
    ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".cs": "csharp",
    ".fs": "fsharp", ".vb": "vbnet", ".swift": "swift", ".m": "objectivec", ".mm": "objectivec", ".rb": "ruby",
    ".php": "php", ".pl": "perl", ".pm": "perl", ".lua": "lua", ".r": "r", ".jl": "julia", ".dart": "dart",
    ".ex": "elixir", ".exs": "elixir", ".erl": "erlang", ".hrl": "erlang", ".clj": "clojure", ".hs": "haskell",
    ".ml": "ocaml", ".elm": "elm", ".zig": "zig", ".nim": "nim", ".sol": "solidity", ".sh": "bash",
    ".bash": "bash", ".zsh": "bash", ".fish": "fish", ".ps1": "powershell", ".bat": "batch", ".cmd": "batch",
    ".sql": "sql", ".graphql": "graphql", ".gql": "graphql", ".proto": "protobuf", ".thrift": "thrift",
    ".tf": "hcl", ".tfvars": "hcl", ".hcl": "hcl", ".nix": "nix", ".cmake": "cmake", ".html": "html",
    ".htm": "html", ".css": "css", ".scss": "scss", ".sass": "sass", ".less": "less", ".xml": "xml",
    ".xsd": "xml", ".json": "json", ".jsonc": "json", ".json5": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".conf": "ini", ".properties": "properties",
    ".md": "markdown", ".mdx": "markdown", ".rst": "rst", ".txt": "text", ".adoc": "asciidoc", ".tex": "latex",
    ".prisma": "prisma", ".http": "http",
}
NAMED = {
    "dockerfile": "dockerfile", "containerfile": "dockerfile", "makefile": "makefile", "gnumakefile": "makefile",
    "cmakelists.txt": "cmake", "jenkinsfile": "groovy", "procfile": "text", "gemfile": "ruby",
    "rakefile": "ruby", "vagrantfile": "ruby", "justfile": "makefile", ".env.example": "ini",
    ".env.sample": "ini", "codeowners": "text", ".gitignore": "text", ".dockerignore": "text",
}
DATA_LANGS = {"json", "yaml", "xml", "toml"}


def language(path: str) -> str | None:
    name = PurePosixPath(path).name.lower()
    if name in NAMED:
        return NAMED[name]
    if name.startswith("dockerfile"):
        return "dockerfile"
    return LANGS.get(PurePosixPath(name).suffix)


def _candidate(path: str, size: int) -> str | None:
    """Language if this path should be indexed, judged on name and size alone."""
    parts = path.split("/")
    if any(p in SKIP_DIRS or p.endswith(".egg-info") for p in parts[:-1]):
        return None
    name = parts[-1]
    if name in SKIP_FILES or name.endswith(SKIP_SUFFIXES) or size > MAX_FILE_BYTES:
        return None
    lang = language(path)
    if lang in DATA_LANGS and size > MAX_JSON_BYTES:
        return None
    return lang


def _decode(data: bytes) -> str | None:
    """Text, or None for binary / minified content."""
    if b"\0" in data[:8192]:
        return None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    lines = text.count("\n") + 1
    if len(text) > 4000 and len(text) / lines > 300:   # minified / generated single-line blobs
        return None
    return text.replace("\r\n", "\n")


class _Ignore:
    """.gitignore rules from every directory, applied to paths beneath it."""

    def __init__(self) -> None:
        self._specs: list[tuple[str, pathspec.PathSpec]] = []

    def add(self, gitignore_path: str, text: str) -> None:
        base = str(PurePosixPath(gitignore_path).parent)
        base = "" if base == "." else base + "/"
        try:
            self._specs.append((base, pathspec.GitIgnoreSpec.from_lines(text.splitlines())))
        except Exception:  # noqa: BLE001 — a malformed .gitignore must not fail the import
            log.debug("bad .gitignore %s", gitignore_path)

    def ignored(self, path: str) -> bool:
        return any(path.startswith(base) and spec.match_file(path[len(base):]) for base, spec in self._specs)


@dataclass
class ScanResult:
    files: list[dict] = field(default_factory=list)
    skipped: int = 0          # files left out by the size/count limits (not the junk filters)
    venv_dirs: set[str] = field(default_factory=set)

    def add(self, path: str, lang: str, data: bytes) -> bool:
        """False once a limit is hit."""
        if len(self.files) >= MAX_FILES or self.bytes + len(data) > MAX_TOTAL_BYTES:
            self.skipped += 1
            return False
        text = _decode(data)
        if text is None or not text.strip():
            return True
        text, _ = secretscan.redact(text)   # credentials committed to a repo never reach the index or the model
        self.files.append({"path": path, "lang": lang, "size": len(data), "lines": text.count("\n") + 1,
                           "sha": hashlib.sha1(data).hexdigest(), "text": text})
        self.bytes += len(data)
        return True

    bytes: int = 0


def _select(entries: list[tuple[str, int]], read) -> ScanResult:
    """entries: (posix path, size). Applies .gitignore files, virtualenv detection and the junk filters;
    source files are taken before docs so the limits cut the least useful files."""
    res = ScanResult()
    ign = _Ignore()
    for path, size in entries:
        name = PurePosixPath(path).name
        if name == ".gitignore" and size < 256 * 1024:
            ign.add(path, (read(path) or b"").decode("utf-8", "replace"))
        elif name == "pyvenv.cfg":
            res.venv_dirs.add(str(PurePosixPath(path).parent) + "/")
    picked: list[tuple[int, str, str]] = []
    for path, size in entries:
        lang = _candidate(path, size)
        if not lang or any(path.startswith(v) for v in res.venv_dirs) or ign.ignored(path):
            continue
        prio = 2 if lang in ("markdown", "rst", "text", "asciidoc") else 1 if lang in DATA_LANGS else 0
        picked.append((prio, path, lang))
    picked.sort(key=lambda t: (t[0], t[1].count("/"), t[1]))
    for _, path, lang in picked:
        data = read(path)
        if data is not None:
            res.add(path, lang, data)
    res.files.sort(key=lambda f: f["path"])
    return res


def scan_zip(zip_path: Path) -> ScanResult:
    """Reads members directly (nothing is extracted, so no zip-slip). A single top-level folder, as in
    GitHub's "Download ZIP", is stripped."""
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ZIP_ENTRIES:
            raise ValueError(f"The archive has {len(infos):,} entries; the limit is {MAX_ZIP_ENTRIES:,}")
        infos = [i for i in infos if not stat.S_ISLNK(i.external_attr >> 16)]  # symlinks
        names = [i.filename.replace("\\", "/").lstrip("/") for i in infos]
        tops = {n.split("/", 1)[0] for n in names}
        strip = len(tops) == 1 and all("/" in n for n in names)
        by_path: dict[str, zipfile.ZipInfo] = {}
        for i, n in zip(infos, names):
            p = n.split("/", 1)[1] if strip else n
            if p and ".." not in PurePosixPath(p).parts and "__MACOSX" not in p:
                by_path[p] = i

        def read(p: str) -> bytes | None:
            info = by_path[p]
            if info.file_size > MAX_FILE_BYTES and PurePosixPath(p).name != ".gitignore":
                return None
            with zf.open(info) as fh:
                return fh.read(MAX_FILE_BYTES + 1)

        return _select([(p, i.file_size) for p, i in by_path.items()], read)


def scan_dir(root: Path) -> ScanResult:
    """Walks a checkout without following symlinks (a repo could link to files outside itself)."""
    entries: list[tuple[str, int]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            full = Path(dirpath, fn)
            st = full.lstat()
            if stat.S_ISREG(st.st_mode):
                entries.append((full.relative_to(root).as_posix(), st.st_size))

    def read(p: str) -> bytes | None:
        full = root / p
        return None if full.is_symlink() else full.read_bytes()

    return _select(entries, read)


# ---------------------------------------------------------------- git
class FetchError(Exception):
    pass


def normalise_git_url(url: str) -> tuple[str, str | None]:
    """https URL without credentials, plus a token if one was embedded (https://user:token@host/...)."""
    u = urlsplit(url.strip())
    if u.scheme != "https" or not u.hostname:
        raise ValueError("Use an https:// clone URL (for example https://github.com/org/repo.git)")
    token = u.password or (u.username if u.username and not u.password else None)
    host = u.hostname + (f":{u.port}" if u.port else "")
    return urlunsplit(("https", host, u.path.rstrip("/"), "", "")), token


def repo_name_from(url_or_filename: str) -> str:
    stem = PurePosixPath(urlsplit(url_or_filename).path or url_or_filename).name
    for suffix in (".git", ".zip"):
        stem = stem.removesuffix(suffix)
    stem = re.sub(r"[^\w.\-]+", "-", stem).strip("-.")
    return stem[:60] or "repo"


async def _git(*args: str, cwd: Path | None = None, env: dict | None = None, timeout: float = 60) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise FetchError(f"git {args[0]} timed out after {int(timeout)}s") from None
    if proc.returncode:
        msg = err.decode(errors="replace").strip().splitlines()
        raise FetchError(next((m for m in reversed(msg) if m.startswith(("fatal", "error", "remote"))),
                              msg[-1] if msg else f"git {args[0]} failed"))
    return out.decode().strip()


def git_env(token: str | None = None) -> dict[str, str]:
    """Environment for network git commands: https only, no prompts, no system config, symlinks checked out
    as plain files. The token travels in an environment-supplied http header, so it is never visible in the
    process list or written to disk."""
    cfg = [("protocol.allow", "never"), ("protocol.https.allow", "always"), ("core.symlinks", "false")]
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cfg.append(("http.extraHeader", f"Authorization: Basic {basic}"))
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1", "GIT_ASKPASS": "/bin/false",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_COUNT": str(len(cfg))}
    for i, (k, v) in enumerate(cfg):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = k, v
    return env


def friendly_git_error(msg: str, ref: str | None = None) -> str:
    if "could not read Username" in msg or "Authentication failed" in msg or "403" in msg:
        return "The repository is private or the URL is wrong. Add an access token with read access to its contents."
    if "Remote branch" in msg and "not found" in msg:
        return f"Branch or tag “{ref}” was not found in the repository."
    if "not found" in msg:
        return "Repository not found. Check the URL; if the repository is private, add an access token."
    return msg


async def clone(url: str, ref: str | None, token: str | None, dest: Path, git_dir: Path | None = None) -> tuple[str, str]:
    """Shallow clone of one branch/tag. Returns (commit sha, branch name).
    `git_dir` keeps the repository data outside the working tree (project workspaces)."""
    env = git_env(token)
    args = ["clone", "--depth", "1", "--single-branch", "--no-tags", "--quiet"]
    if ref:
        args += ["--branch", ref]
    if git_dir:
        args += ["--separate-git-dir", str(git_dir)]
    try:
        await _git(*args, "--", url, str(dest), env=env, timeout=CLONE_TIMEOUT_S)
    except FetchError as e:
        raise FetchError(friendly_git_error(str(e), ref)) from None
    sha = await _git("rev-parse", "HEAD", cwd=dest)
    branch = await _git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest)
    return sha, branch


def pick_remote_sha(ls_remote_output: str, ref: str | None) -> str | None:
    """The commit a branch, tag or HEAD points to, from `git ls-remote` output ("<sha>\t<refname>" lines).

    Preference: the branch, then the commit an annotated tag points to (the peeled `^{}` line), then a
    lightweight tag. Clones record the commit, so this is what to compare against."""
    refs = {}
    for line in ls_remote_output.splitlines():
        sha, _, name = line.partition("\t")
        if sha and name:
            refs[name.strip()] = sha.strip()
    if not ref or ref == "HEAD":
        return refs.get("HEAD")
    for name in (f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}"):
        if name in refs:
            return refs[name]
    return None


async def remote_head(url: str, ref: str | None, token: str | None) -> str:
    """Latest commit of `ref` (or the default branch) on the remote, without fetching any objects."""
    patterns = ["HEAD"] if not ref or ref == "HEAD" else [f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"]
    try:
        out = await _git("ls-remote", "--", url, *patterns, env=git_env(token), timeout=LS_REMOTE_TIMEOUT_S)
    except FetchError as e:
        raise FetchError(friendly_git_error(str(e), ref)) from None
    sha = pick_remote_sha(out, ref)
    if not sha:
        raise FetchError(f"Branch or tag “{ref}” was not found in the repository." if ref and ref != "HEAD"
                         else "The repository has no default branch.")
    return sha


# ---------------------------------------------------------------- chunking
_DEF = re.compile(
    r"^(?:(?:export|default|pub(?:\([^)]*\))?|public|private|protected|internal|static|abstract|final|async|"
    r"override|open|data|sealed|inline|unsafe|extern|partial|virtual)\s+)*"
    r"(?:def|class|function|func|fn|impl|trait|interface|type|struct|enum|union|module|object|record|namespace|"
    r"const|let|var|val|fun|sub|procedure|macro|mod|describe|it|test|CREATE|create|ALTER|alter)\b")
_DOC_HEADING = re.compile(r"^#{1,4}\s")
_LEAD = re.compile(r"^\s*(?:@|#\[|//|#(?!\s*include)|/\*|\*|--|\"\"\"|''')")
_DOC_LANGS = {"markdown", "rst", "text", "asciidoc"}


def _break_scores(lines: list[str], lang: str) -> list[int]:
    """How good a place each line is to START a chunk: 3 top-level definition / heading, 2 nested
    definition, 1 after a blank line, 0 mid-block."""
    scores = [0] * len(lines)
    doc = lang in _DOC_LANGS
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        prev = lines[i - 1].strip() if i else ""
        if doc:
            scores[i] = 3 if _DOC_HEADING.match(line) else 1 if not prev else 0
        elif indent == 0 and (_DEF.match(stripped) or prev in ("}", "};", "end")):
            scores[i] = 3
        elif indent <= 8 and _DEF.match(stripped):
            scores[i] = 2
        elif not prev and stripped:
            scores[i] = 1
    return scores


def chunk_code(text: str, lang: str) -> list[tuple[int, int, str]]:
    """Line-aligned chunks (1-based start, end, text) cut at the best boundaries near CHUNK_TARGET, so a
    chunk is usually whole functions/classes. Comments and decorators stay with what they describe."""
    lines = text.split("\n")
    if len(text) <= CHUNK_MAX:
        return [(1, len(lines), text)] if text.strip() else []
    scores = _break_scores(lines, lang)
    sizes = [len(l) + 1 for l in lines]
    out: list[tuple[int, int, str]] = []
    start, size, i = 0, 0, 0
    best_cut, best_score = -1, -1

    def emit(end: int) -> None:  # [start, end)
        body = "\n".join(lines[start:end])
        if body.strip():
            out.append((start + 1, end, body))

    while i < len(lines):
        if i > start and size >= CHUNK_MIN:
            # Pull the cut point up over decorators / doc comments that belong to the definition.
            s = scores[i]
            if s >= 2:
                j = i
                while j - 1 > start and lines[j - 1].strip() and _LEAD.match(lines[j - 1]):
                    j -= 1
                cut = j
            else:
                cut = i
            if s >= best_score:
                best_cut, best_score = cut, s
            want = {3: CHUNK_TARGET * 0.5, 2: CHUNK_TARGET * 0.8, 1: CHUNK_TARGET, 0: CHUNK_MAX}[s]
            if size >= want:
                emit(cut)
                start, size, i = cut, sum(sizes[cut:i]), i
                best_cut, best_score = -1, -1
                continue
        if size + sizes[i] > CHUNK_MAX and i > start:
            cut = best_cut if best_cut > start else i
            emit(cut)
            start, size = cut, sum(sizes[cut:i])
            best_cut, best_score = -1, -1
            continue
        if sizes[i] > CHUNK_MAX:  # a single giant line: emit it alone, truncated
            if i > start:
                emit(i)
            out.append((i + 1, i + 1, lines[i][:CHUNK_MAX]))
            start, size, i = i + 1, 0, i + 1
            continue
        size += sizes[i]
        i += 1
    if start < len(lines):
        emit(len(lines))
    return out


def layout_summary(paths: list[str], max_entries: int = 60) -> str:
    """Compact two-level tree with file counts, for the system prompt."""
    top: Counter[str] = Counter()
    sub: dict[str, Counter[str]] = {}
    root_files: list[str] = []
    for p in paths:
        parts = p.split("/")
        if len(parts) == 1:
            root_files.append(p)
            continue
        top[parts[0]] += 1
        if len(parts) > 2:
            sub.setdefault(parts[0], Counter())[parts[1]] += 1
    lines: list[str] = []
    for d, n in sorted(top.items()):
        lines.append(f"{d}/ ({n} files)")
        for s, m in sorted(sub.get(d, Counter()).items(), key=lambda kv: -kv[1])[:8]:
            lines.append(f"  {d}/{s}/ ({m})")
    lines += root_files[:20]
    if len(lines) > max_entries:
        lines = lines[:max_entries] + [f"… ({len(lines) - max_entries} more entries)"]
    return "\n".join(lines)


# ---------------------------------------------------------------- service
class RepoService:
    def __init__(self, db: Database, kb: KnowledgeBase, work_dir: Path, auto_sync_s: float = 0):
        self.db = db
        self.auto_sync_s = auto_sync_s          # 0: git repos only sync when someone asks
        self.kb = kb
        self.work = work_dir
        self.work.mkdir(parents=True, exist_ok=True)
        self.worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._sem = asyncio.Semaphore(JOB_CONCURRENCY)
        self._tasks: dict[str, asyncio.Task] = {}
        self._sweeper: asyncio.Task | None = None
        self._auto: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self.kb.enabled

    def zip_path(self, rid: str) -> Path:
        return self.work / f"{rid}.zip"

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        self._sweeper = asyncio.create_task(self._sweep_forever())
        if self.auto_sync_s > 0 and self.enabled:
            self._auto = asyncio.create_task(self._auto_sync_forever())

    async def stop(self) -> None:
        tasks = [t for t in [self._sweeper, self._auto, *self._tasks.values()] if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- automatic sync -------------------------------------------------------
    async def _auto_sync_forever(self) -> None:
        """Check git repos for new commits every `auto_sync_s`. Both server processes run this loop; the
        check of each repo is claimed atomically in the database, so it runs once per interval in total."""
        await asyncio.sleep(AUTO_SYNC_TICK_S / 2)     # let start-up (and job resumption) settle first
        sem = asyncio.Semaphore(AUTO_SYNC_CONCURRENCY)
        while True:
            try:
                before = time.time() - self.auto_sync_s
                due = [r for r in self.db.repos_due_for_check(before) if self.db.claim_repo_check(r["id"], before)]
                if due:
                    await asyncio.gather(*(self._check_remote(r, sem) for r in due))
            except Exception:   # the loop must survive transient DB errors
                log.exception("repo auto-sync pass failed")
            await asyncio.sleep(AUTO_SYNC_TICK_S)

    async def _check_remote(self, repo: dict, sem: asyncio.Semaphore) -> None:
        rid = repo["id"]
        meta = repo["meta"] or {}
        async with sem:
            try:
                head = await remote_head(repo["url"], repo["ref"], repo["auth_token"])
            except FetchError as e:
                # A slow or flaky link fails now and then; only a repeated failure (revoked token, deleted
                # repository, renamed branch) is shown on the card and on Admin → Health. The index stays usable.
                failures = int(meta.get("auto_sync_failures") or 0) + 1
                log.warning("auto-sync check of repo %s failed (%d in a row): %s", repo["name"], failures, e)
                patch: dict = {"auto_sync_failures": failures}
                if failures >= AUTO_SYNC_REPORT_AFTER:
                    patch["auto_sync_error"] = str(e)[:300]
                self.db.merge_repo_meta(rid, patch)
                return
        if meta.get("auto_sync_error") or meta.get("auto_sync_failures"):
            self.db.merge_repo_meta(rid, {"auto_sync_error": None, "auto_sync_failures": None})
        if head == repo["commit_sha"]:
            return
        if repo["status"] == "error" and meta.get("attempted_commit") == head:
            return        # this exact commit already failed to index; wait for a new one (or a manual retry)
        if self.db.queue_repo_sync(rid):
            self.db.merge_repo_meta(rid, {"sync_trigger": "auto"})
            log.info("auto-sync: repo %s has a new commit (%s → %s), syncing", repo["name"],
                     (repo["commit_sha"] or "none")[:7], head[:7])
            self.schedule(rid)

    async def _sweep_forever(self) -> None:
        while True:
            try:
                for rid in self.db.unfinished_repos():
                    self.schedule(rid)
            except Exception:  # noqa: BLE001 — the sweeper must survive transient DB errors
                log.exception("repo sweep failed")
            await asyncio.sleep(SWEEP_EVERY_S)

    def schedule(self, rid: str) -> bool:
        """Start the job here if no process holds it. False if it is already running somewhere."""
        if rid in self._tasks or not self.db.claim_repo(rid, self.worker_id, LEASE_TTL_S):
            return False
        task = asyncio.create_task(self._run(rid))
        self._tasks[rid] = task
        task.add_done_callback(lambda _t: self._tasks.pop(rid, None))
        return True

    def busy(self, rid: str) -> bool:
        r = self.db.get_repo(rid, private=True)
        return bool(r and r["status"] in ("queued", "fetching", "indexing") and r["lease_until"]
                    and r["lease_until"] > time.time())

    async def cancel(self, rid: str) -> None:
        if (t := self._tasks.get(rid)):
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
        self.zip_path(rid).unlink(missing_ok=True)

    # -- the job --------------------------------------------------------------
    def _renew(self, rid: str) -> None:
        if not self.db.claim_repo(rid, self.worker_id, LEASE_TTL_S):
            raise asyncio.CancelledError("lease lost")

    async def _heartbeat(self, rid: str, job: asyncio.Task) -> None:
        """Keep the lease alive through long steps (clone, big embedding batches); stop the job if it is lost."""
        while True:
            await asyncio.sleep(LEASE_TTL_S / 3)
            if not self.db.claim_repo(rid, self.worker_id, LEASE_TTL_S):
                log.warning("lost lease on repo %s; stopping", rid)
                job.cancel()
                return

    async def _run(self, rid: str) -> None:
        async with self._sem:
            hb = asyncio.create_task(self._heartbeat(rid, asyncio.current_task()))
            try:
                repo = self.db.get_repo(rid, private=True)
                if not repo:
                    return
                if repo["status"] in ("queued", "fetching"):
                    await self._fetch(repo)
                await self._index(rid, repo["project_id"])
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — shown on the repo card; indexed files stay usable
                if isinstance(e, (FetchError, ValueError)):
                    log.warning("repo job %s: %s", rid, e)
                else:
                    log.exception("repo job %s failed", rid)
                if self.db.get_repo(rid):
                    self.db.update_repo(rid, {"status": "error"})
                    self.db.merge_repo_meta(rid, {"error": str(e)[:300] or type(e).__name__})
            finally:
                hb.cancel()
                self.db.release_repo(rid, self.worker_id)
                self.kb.invalidate(self.db.get_repo(rid)["project_id"] if self.db.get_repo(rid) else "")

    async def _fetch(self, repo: dict) -> None:
        rid = repo["id"]
        self.db.update_repo(rid, {"status": "fetching"})
        self.db.merge_repo_meta(rid, {"error": None, "phase_started": time.time()})
        if repo["source"] == "zip":
            zp = self.zip_path(rid)
            if not zp.exists():
                raise FetchError("The uploaded archive is gone; upload it again")
            try:
                scan = await asyncio.to_thread(scan_zip, zp)
            except zipfile.BadZipFile:
                raise FetchError("This file is not a valid .zip archive") from None
            commit, branch = None, None
        else:
            tmp = Path(tempfile.mkdtemp(prefix=f"clone-{rid[:8]}-", dir=self.work))
            try:
                commit, branch = await clone(repo["url"], repo["ref"], repo["auth_token"], tmp / "src")
                # Lets auto-sync tell "this commit failed to index" from "there is a newer commit".
                self.db.merge_repo_meta(rid, {"attempted_commit": commit})
                self._renew(rid)
                scan = await asyncio.to_thread(scan_dir, tmp / "src")
            finally:
                await asyncio.to_thread(shutil.rmtree, tmp, True)
        if not scan.files:
            raise FetchError("No source files were found (binary files, dependencies and build output are skipped)")
        self._renew(rid)
        diff = await asyncio.to_thread(self.db.sync_repo_files, rid, scan.files)
        fields: dict = {"status": "indexing"}
        if commit:
            fields.update(commit_sha=commit, ref=repo["ref"] or branch)
        self.db.update_repo(rid, fields)
        self.db.merge_repo_meta(rid, {"skipped": scan.skipped, "changes": diff, "synced_at": time.time()})
        if repo["source"] == "zip":
            self.zip_path(rid).unlink(missing_ok=True)
        log.info("repo %s fetched: %d files (%s)", repo["name"], len(scan.files), diff)

    async def _index(self, rid: str, pid: str) -> None:
        repo = self.db.get_repo(rid)
        if not repo:
            return
        self.db.update_repo(rid, {"status": "indexing"})
        name = repo["name"]
        stats = self.db.repo_stats(rid)
        self.db.merge_repo_meta(rid, {"files": stats["files"], "indexed": stats["indexed"]})
        while (batch := self.db.pending_repo_files(rid, FILES_PER_BATCH)):
            self._renew(rid)
            per_file = [chunk_code(f["text"], f["lang"] or "text") for f in batch]
            inputs = [f"File: {name}/{f['path']} (lines {s}-{e})\n\n{t}"
                      for f, chunks in zip(batch, per_file) for s, e, t in chunks]
            vecs = await self.kb.embed(inputs) if inputs else np.zeros((0, 1), np.float32)
            k = 0
            for f, chunks in zip(batch, per_file):
                rows = []
                for s, e, t in chunks:
                    rows.append((f"{name}/{f['path']}", s, e, t, vecs[k].astype(np.float16).tobytes()))
                    k += 1
                await asyncio.to_thread(self.db.replace_file_chunks, f["id"], pid, f["sha"], rows)
            stats = self.db.repo_stats(rid)
            self.db.merge_repo_meta(rid, {"files": stats["files"], "indexed": stats["indexed"],
                                          "chunks": stats["chunks"]})
        stats = self.db.repo_stats(rid)
        listing = self.db.repo_listing(rid)
        langs = Counter()
        for f in listing:
            if f["lang"] not in _DOC_LANGS | DATA_LANGS | {None}:
                langs[f["lang"]] += f["lines"]
        self.db.merge_repo_meta(rid, {
            "files": stats["files"], "indexed": stats["indexed"], "chunks": stats["chunks"], "bytes": stats["bytes"],
            "lines": stats["lines"], "langs": [l for l, _ in langs.most_common(5)],
            "tree": layout_summary([f["path"] for f in listing]), "error": None})
        self.db.update_repo(rid, {"status": "ready"})
        log.info("repo %s indexed: %d files, %d chunks", name, stats["files"], stats["chunks"])


# ---------------------------------------------------------------- chat tools over a project's code
READ_DEFAULT_LINES = 300
READ_MAX_LINES = 800
LIST_MAX_ENTRIES = 300
GREP_MAX_MATCHES = 120


class ProjectCode:
    """read_file / list_files / grep_code bound to one project. Paths are "<repo>/<path>"."""

    def __init__(self, db: Database, pid: str):
        self.db = db
        self.pid = pid

    def _resolve(self, path: str) -> dict | None:
        path = path.strip().lstrip("./").strip("/")
        if (f := self.db.project_code_file(self.pid, path)):
            return f
        # Lenient: a path without the repo prefix, or a unique suffix match.
        listing = [r["path"] for r in self.db.project_code_listing(self.pid)]
        matches = [p for p in listing if p.endswith("/" + path)]
        return self.db.project_code_file(self.pid, matches[0]) if len(matches) == 1 else None

    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        f = self._resolve(path)
        if not f:
            return f"Error: no file {path!r} in this project's repositories. Use list_files or grep_code to find it."
        lines = f["text"].split("\n")
        start = max(1, start_line or 1)
        end = min(len(lines), end_line or start + READ_DEFAULT_LINES - 1, start + READ_MAX_LINES - 1)
        if start > len(lines):
            return f"Error: {f['path']} has only {len(lines)} lines"
        width = len(str(end))
        body = "\n".join(f"{n:>{width}}| {lines[n - 1]}" for n in range(start, end + 1))
        more = (f"\n… (showing lines {start}-{end} of {len(lines)}; call read_file with start_line={end + 1} "
                "for more)") if end < len(lines) else ""
        return f"{f['path']} ({f['lang'] or 'text'}, {len(lines)} lines)\n{body}{more}"

    def list_files(self, path: str = "", depth: int = 2) -> str:
        prefix = path.strip().strip("/")
        prefix = f"{prefix}/" if prefix else ""
        listing = self.db.project_code_listing(self.pid)
        rows = [r for r in listing if r["path"].startswith(prefix)]
        if not rows and prefix:
            alt = [r for r in listing if f"/{prefix}" in f"/{r['path']}"]
            if alt:
                return f"Error: {path!r} is not a top-level path. Did you mean " \
                       f"{alt[0]['path'][:alt[0]['path'].find(prefix) + len(prefix)]!r}?"
            return f"Error: nothing under {path!r}"
        depth = max(1, min(depth, 6))
        dirs: Counter[str] = Counter()
        files: list[str] = []
        for r in rows:
            rel = r["path"][len(prefix):].split("/")
            if len(rel) <= depth:
                files.append(f"{prefix}{'/'.join(rel)}  ({r['lines']} lines)")
            else:
                dirs[prefix + "/".join(rel[:depth]) + "/"] += 1
        entries = sorted([f"{d}  ({n} files)" for d, n in dirs.items()] + files)
        extra = f"\n… {len(entries) - LIST_MAX_ENTRIES} more" if len(entries) > LIST_MAX_ENTRIES else ""
        return f"{len(rows)} files under {prefix or 'the project root'}\n" + "\n".join(entries[:LIST_MAX_ENTRIES]) + extra

    def grep(self, pattern: str, path_glob: str | None = None, regex: bool = False,
             ignore_case: bool = True) -> str:
        if not pattern or len(pattern) > 300:
            return "Error: pattern must be 1-300 characters"
        flags = re.IGNORECASE if ignore_case else 0
        try:
            rx = re.compile(pattern if regex else re.escape(pattern), flags)
        except re.error as e:
            return f"Error: invalid regex: {e}"
        globs = [g.strip() for g in (path_glob or "").split(",") if g.strip()]
        out: list[str] = []
        files_hit = 0
        total = 0
        for path, text in self.db.iter_project_code(self.pid):
            if globs and not any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, f"*/{g}") for g in globs):
                continue
            if not rx.search(text):
                continue
            files_hit += 1
            for n, line in enumerate(text.split("\n"), 1):
                if rx.search(line):
                    total += 1
                    if len(out) < GREP_MAX_MATCHES:
                        out.append(f"{path}:{n}: {line.strip()[:200]}")
        if not out:
            return f"No matches for {pattern!r}" + (f" in {path_glob}" if path_glob else "")
        more = f"\n… {total - len(out)} more matches; narrow with path_glob" if total > len(out) else ""
        return f"{total} matches in {files_hit} files\n" + "\n".join(out) + more
