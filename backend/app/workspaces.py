"""Project workspaces: writable working copies on the server that the model edits like a developer would.

A workspace is a git working tree under data/workspaces/<id>/:
    work/   the files — mounted read-write at /work in the sandbox when the model runs commands
    git/    the repository data, kept OUTSIDE the working tree. The sandbox only ever sees it read-only, so
            nothing the model writes can plant hooks or config that the server's own git commands would run.
    ctl/    scratch for the server (scripts handed to the sandbox, temporary index files)

The model works through file tools (list/read/write/edit/grep, executed here on the host with strict path
checks: no "..", no following symlinks out of the tree) and run_shell (executed in the sandbox: no network).
The owner reviews the changes as a diff and downloads them (patch / zip) or commits and pushes to a branch.
"""
from __future__ import annotations

import asyncio
import difflib
import fnmatch
import logging
import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .db import Database
from .repos import FetchError, clone, friendly_git_error, git_env
from .sandbox import Sandbox, clip, describe
from .tools import ToolError, ToolResult

log = logging.getLogger("qwen.workspaces")

MAX_WORKSPACES_PER_USER = 20
MAX_READ_BYTES = 2 * 2**20
MAX_WRITE_BYTES = 1 * 2**20
READ_DEFAULT_LINES = 400
READ_MAX_LINES = 1000
LIST_MAX_ENTRIES = 400
GREP_MAX_MATCHES = 200
GREP_MAX_FILE_BYTES = 1 * 2**20
MAX_DIFF_CHARS = 400_000          # full diff returned to the UI
TOOL_DIFF_CHARS = 6_000           # diff attached to a write/edit tool block
MAX_CHANGED_LISTED = 500
SHELL_MEMORY = "4g"
GIT_TIMEOUT_S = 120
STALE_CREATING_S = 900
# Directories never walked by list/grep (still reachable by explicit path).
NOISE_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv", "venv",
              ".tox", ".next", ".nuxt", "dist", "build", "target", ".gradle", ".idea", ".cache", "coverage",
              ".turbo", ".parcel-cache", "bower_components", ".terraform"}
_BRANCH = re.compile(r"^(?!.*\.\.)(?!.*//)(?!/)(?!.*/$)(?!.*\.lock$)[A-Za-z0-9._/\-]{1,120}$")


class WorkspaceError(ToolError):
    """A problem worth showing to the user or the model as is."""


# ====================================================================== file tools
class WorkspaceFS:
    """The model's file tools for one working tree. Every path is resolved inside the tree; symlinks are never
    followed out of it (the sandbox can create symlinks, and these methods run on the host)."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    # -- paths ----------------------------------------------------------------
    def _rel(self, path: str) -> PurePosixPath:
        p = (path or "").strip().replace("\\", "/")
        for prefix in ("/work/", "work/", "./"):
            if p.startswith(prefix):
                p = p[len(prefix):]
        p = p.lstrip("/")
        rel = PurePosixPath(p) if p not in ("", ".", "/work") else PurePosixPath(".")
        if any(part == ".." for part in rel.parts):
            raise WorkspaceError("Paths must stay inside the workspace (no '..')")
        return rel

    def _inside(self, p: Path) -> bool:
        try:
            Path(os.path.realpath(p)).relative_to(self.root)
            return True
        except ValueError:
            return False

    def _existing(self, path: str) -> tuple[PurePosixPath, Path]:
        rel = self._rel(path)
        p = self.root / rel
        if not os.path.lexists(p):
            raise WorkspaceError(f"{rel} does not exist")
        if not self._inside(p):
            raise WorkspaceError(f"{rel} points outside the workspace")
        return rel, p

    def _writable(self, path: str) -> tuple[PurePosixPath, Path]:
        """Target for a write: every existing ancestor must be a real directory inside the tree."""
        rel = self._rel(path)
        if rel == PurePosixPath("."):
            raise WorkspaceError("A file path is required")
        if rel.parts[0] == ".git":
            raise WorkspaceError("The .git path is reserved")
        cur = self.root
        for part in rel.parts[:-1]:
            cur = cur / part
            if os.path.islink(cur):
                raise WorkspaceError(f"{cur.relative_to(self.root)} is a symbolic link; write to the real path")
            if cur.exists() and not cur.is_dir():
                raise WorkspaceError(f"{cur.relative_to(self.root)} is a file, not a folder")
        target = self.root / rel
        if os.path.islink(target):
            raise WorkspaceError(f"{rel} is a symbolic link; write to the real path")
        if target.is_dir():
            raise WorkspaceError(f"{rel} is a folder")
        return rel, target

    @staticmethod
    def _is_binary(head: bytes) -> bool:
        return b"\0" in head[:8192]

    def _read_text(self, p: Path, rel: PurePosixPath) -> str:
        size = p.stat().st_size
        if size > MAX_READ_BYTES:
            raise WorkspaceError(f"{rel} is too large to read as text ({size:,} bytes)")
        data = p.read_bytes()
        if self._is_binary(data):
            raise WorkspaceError(f"{rel} is a binary file ({size:,} bytes)")
        return data.decode("utf-8", errors="replace")

    @staticmethod
    def _diff(rel: PurePosixPath, old: str | None, new: str) -> tuple[str, int, int]:
        a = (old or "").splitlines(keepends=True)
        b = new.splitlines(keepends=True)
        lines = list(difflib.unified_diff(a, b, f"a/{rel}" if old is not None else "/dev/null", f"b/{rel}", n=3))
        added = sum(1 for x in lines if x.startswith("+") and not x.startswith("+++"))
        removed = sum(1 for x in lines if x.startswith("-") and not x.startswith("---"))
        text = "".join(x if x.endswith("\n") else x + "\n\\ No newline at end of file\n" for x in lines)
        return clip(text, TOOL_DIFF_CHARS), added, removed

    def _atomic_write(self, target: Path, text: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
        tmp = target.with_name(f".{target.name}.qd-{uuid.uuid4().hex[:8]}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- tools ----------------------------------------------------------------
    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        rel, p = self._existing(path)
        if p.is_dir():
            return self.list_files(path, 1)
        lines = self._read_text(p, rel).splitlines()
        n = len(lines)
        start = max(1, start_line or 1)
        end = min(n, end_line or start + READ_DEFAULT_LINES - 1, start + READ_MAX_LINES - 1)
        if n == 0:
            return f"{rel} (empty file)"
        if start > n:
            raise WorkspaceError(f"{rel} has only {n} lines")
        width = len(str(end))
        body = "\n".join(f"{i:>{width}}  {lines[i - 1]}" for i in range(start, end + 1))
        more = f"\n… lines {end + 1}-{n} not shown; call read_file with start_line={end + 1} to continue" if end < n else ""
        return f"{rel} (lines {start}-{end} of {n})\n{body}{more}"

    def list_files(self, path: str = "", depth: int = 2) -> str:
        rel, base = self._existing(path or ".")
        if not base.is_dir():
            return f"{rel} is a file ({base.stat().st_size:,} bytes)"
        depth = max(1, min(int(depth or 2), 6))
        out: list[str] = []

        def walk(d: Path, level: int) -> None:
            try:
                entries = sorted(os.scandir(d), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
            except OSError:
                return
            for e in entries:
                if len(out) >= LIST_MAX_ENTRIES:
                    return
                indent = "  " * level
                if e.is_symlink():
                    out.append(f"{indent}{e.name} -> {os.readlink(e.path)}")
                elif e.is_dir():
                    if e.name in NOISE_DIRS:
                        out.append(f"{indent}{e.name}/ (not expanded)")
                        continue
                    count = sum(1 for _ in os.scandir(e.path)) if level + 1 >= depth else None
                    out.append(f"{indent}{e.name}/" + (f" ({count} entries)" if count is not None else ""))
                    if level + 1 < depth:
                        walk(Path(e.path), level + 1)
                else:
                    out.append(f"{indent}{e.name}  ({e.stat(follow_symlinks=False).st_size:,} B)")

        walk(base, 0)
        head = "/ (workspace root)" if str(rel) == "." else f"{rel}/"
        if len(out) >= LIST_MAX_ENTRIES:
            out.append(f"… listing stopped at {LIST_MAX_ENTRIES} entries; list a subfolder for more")
        return head + "\n" + ("\n".join(out) if out else "(empty)")

    def write_file(self, path: str, content: str) -> ToolResult:
        if len(content.encode()) > MAX_WRITE_BYTES:
            raise WorkspaceError(f"Content is larger than {MAX_WRITE_BYTES // 2**20} MB")
        rel, target = self._writable(path)
        old = self._read_text(target, rel) if target.exists() else None
        if old == content:
            return ToolResult(f"{rel} already has this content; nothing changed.")
        self._atomic_write(target, content)
        diff, added, removed = self._diff(rel, old, content)
        verb = "Created" if old is None else "Updated"
        lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
        return ToolResult(f"{verb} {rel} ({lines} lines, +{added} -{removed}).",
                          {"file": {"path": str(rel), "created": old is None, "added": added, "removed": removed,
                                    "diff": diff}})

    def edit_file(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> ToolResult:
        rel, target = self._writable(path)
        if not target.exists():
            raise WorkspaceError(f"{rel} does not exist; use write_file to create it")
        if not old_string:
            raise WorkspaceError("old_string is empty; use write_file to replace a whole file")
        if old_string == new_string:
            raise WorkspaceError("old_string and new_string are identical")
        text = self._read_text(target, rel)
        count = text.count(old_string)
        if count == 0:
            hint = ""
            first = old_string.strip().splitlines()[0].strip() if old_string.strip() else ""
            if first and first in text:
                line = text[:text.index(first)].count("\n") + 1
                hint = (f" Its first line appears at line {line}; the whitespace or the following lines differ — "
                        "read_file that region and copy the text exactly.")
            raise WorkspaceError(f"old_string was not found in {rel}.{hint}")
        if count > 1 and not replace_all:
            raise WorkspaceError(f"old_string occurs {count} times in {rel}; include more surrounding lines so it "
                                 "is unique, or set replace_all=true")
        new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        if len(new_text.encode()) > MAX_WRITE_BYTES:
            raise WorkspaceError(f"The edited file would be larger than {MAX_WRITE_BYTES // 2**20} MB")
        self._atomic_write(target, new_text)
        diff, added, removed = self._diff(rel, text, new_text)
        where = f"{count} places" if replace_all and count > 1 else "1 place"
        return ToolResult(f"Edited {rel} ({where}, +{added} -{removed}).",
                          {"file": {"path": str(rel), "created": False, "added": added, "removed": removed,
                                    "diff": diff}})

    def grep(self, pattern: str, path_glob: str | None = None, regex: bool = False, ignore_case: bool = True) -> str:
        if not pattern:
            raise WorkspaceError("Empty pattern")
        flags = re.IGNORECASE if ignore_case else 0
        try:
            rx = re.compile(pattern if regex else re.escape(pattern), flags)
        except re.error as e:
            raise WorkspaceError(f"Invalid regular expression: {e}") from None
        globs = [g.strip() for g in (path_glob or "").split(",") if g.strip()]
        matches: list[str] = []
        files = 0
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in NOISE_DIRS)
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                rel = p.relative_to(self.root).as_posix()
                if globs and not any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(fn, g) for g in globs):
                    continue
                try:
                    st = os.lstat(p)
                    if not stat.S_ISREG(st.st_mode) or st.st_size > GREP_MAX_FILE_BYTES:
                        continue
                    data = p.read_bytes()
                except OSError:
                    continue
                if self._is_binary(data):
                    continue
                files += 1
                for i, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                    if rx.search(line):
                        matches.append(f"{rel}:{i}: {line.strip()[:300]}")
                        if len(matches) >= GREP_MAX_MATCHES:
                            return "\n".join(matches) + f"\n… stopped at {GREP_MAX_MATCHES} matches; narrow the search"
        return "\n".join(matches) if matches else f"No matches in {files} files."


# ====================================================================== git
@dataclass
class Change:
    path: str
    status: str          # A added, M modified, D deleted, R renamed
    added: int | None    # None for binary files
    removed: int | None
    old_path: str | None = None


class WorkspaceService:
    def __init__(self, db: Database, sandbox: Sandbox, root: Path):
        self.db = db
        self.sandbox = sandbox
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}
        self._jobs: set[asyncio.Task] = set()

    # -- layout ---------------------------------------------------------------
    def dirs(self, wid: str) -> tuple[Path, Path, Path]:
        base = self.root / wid
        return base / "work", base / "git", base / "ctl"

    def fs(self, wid: str) -> WorkspaceFS:
        return WorkspaceFS(self.dirs(wid)[0])

    def lock(self, wid: str) -> asyncio.Lock:
        return self._locks.setdefault(wid, asyncio.Lock())

    # -- host git ---------------------------------------------------------------
    def _env(self, wid: str, index: Path | None = None, network_token: str | None | bool = False) -> dict[str, str]:
        work, git, ctl = self.dirs(wid)
        env = git_env(network_token or None) if network_token is not False else {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
        env.update({"GIT_DIR": str(git), "GIT_WORK_TREE": str(work), "HOME": str(ctl), "LC_ALL": "C",
                    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0"})
        if index:
            env["GIT_INDEX_FILE"] = str(index)
        return env

    _HARDEN = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "safe.directory=*",
               "-c", "commit.gpgsign=false", "-c", "core.quotepath=false", "-c", "core.autocrlf=false")

    async def git(self, wid: str, *args: str, index: Path | None = None, token: str | None | bool = False,
                  timeout: float = GIT_TIMEOUT_S, ok_codes: tuple[int, ...] = (0,)) -> str:
        work = self.dirs(wid)[0]
        proc = await asyncio.create_subprocess_exec(
            "git", *self._HARDEN, *args, cwd=work, env=self._env(wid, index, token),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise WorkspaceError(f"git {args[0]} timed out") from None
        if proc.returncode not in ok_codes:
            msg = err.decode(errors="replace").strip().splitlines()
            raise WorkspaceError(next((m for m in reversed(msg) if m.startswith(("fatal", "error", "remote", "!"))),
                                      msg[-1] if msg else f"git {args[0]} failed"))
        return out.decode(errors="replace")

    async def _snapshot_index(self, wid: str) -> Path:
        """A throwaway index holding the working tree as it is now (new, changed and deleted files, honouring
        .gitignore) — so changes can be listed and diffed without touching the real index."""
        ctl = self.dirs(wid)[2]
        idx = ctl / f"snap-{uuid.uuid4().hex[:8]}.index"
        await self.git(wid, "read-tree", "HEAD", index=idx)
        await self.git(wid, "add", "-A", index=idx, timeout=300)
        return idx

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        """Start-up housekeeping: fail workspaces whose creation was interrupted, drop orphaned folders."""
        for w in self.db.stale_creating_workspaces(time.time() - STALE_CREATING_S):
            self.db.update_workspace(w["id"], {"status": "error", "meta": {**(w["meta"] or {}),
                                                                       "error": "Creation was interrupted; delete it and try again."}})
        known = self.db.all_workspace_ids()
        for d in self.root.iterdir():
            if d.is_dir() and d.name not in known and time.time() - d.stat().st_mtime > STALE_CREATING_S:
                shutil.rmtree(d, ignore_errors=True)

    async def stop(self) -> None:
        for t in list(self._jobs):
            t.cancel()
        await asyncio.gather(*self._jobs, return_exceptions=True)

    def create(self, project_id: str, user: dict, name: str, repo: dict | None) -> dict:
        if self.db.count_workspaces(user["id"]) >= MAX_WORKSPACES_PER_USER:
            raise WorkspaceError(f"You have {MAX_WORKSPACES_PER_USER} workspaces; delete one you no longer need first")
        ws = self.db.create_workspace(project_id, user["id"], name, repo["id"] if repo else None,
                                      repo.get("ref") if repo else None)
        task = asyncio.create_task(self._populate(ws["id"], repo["id"] if repo else None, user))
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)
        return ws

    async def _populate(self, wid: str, repo_id: str | None, user: dict) -> None:
        work, git, ctl = self.dirs(wid)
        try:
            for d in (work, ctl):
                d.mkdir(parents=True, exist_ok=True)
            repo = self.db.get_repo(repo_id, private=True) if repo_id else None
            if repo and repo["source"] == "git":
                shutil.rmtree(work)
                sha, branch = await clone(repo["url"], repo.get("ref") or None, repo.get("auth_token"), work, git)
                (work / ".git").unlink(missing_ok=True)   # the tree must not point at the repository data
                await self.git(wid, "config", "user.name", user["display_name"])
                await self.git(wid, "config", "user.email", f"{user['username']}@qwen-desktop.local")
                self.db.update_workspace(wid, {"status": "ready", "base_sha": sha.strip(), "base_ref": branch.strip()})
            else:
                await self.git(wid, "init", "--quiet", "--initial-branch=main")
                await self.git(wid, "config", "user.name", user["display_name"])
                await self.git(wid, "config", "user.email", f"{user['username']}@qwen-desktop.local")
                if repo:   # an uploaded archive: recreate its text files from the index
                    n = await asyncio.to_thread(self._materialise, repo, work)
                    await self.git(wid, "add", "-A", timeout=300)
                    await self.git(wid, "commit", "--quiet", "-m", f"Import {repo['name']} ({n} files)")
                else:
                    await self.git(wid, "commit", "--quiet", "--allow-empty", "-m", "Empty workspace")
                sha = (await self.git(wid, "rev-parse", "HEAD")).strip()
                self.db.update_workspace(wid, {"status": "ready", "base_sha": sha, "base_ref": "main"})
        except asyncio.CancelledError:
            self.db.update_workspace(wid, {"status": "error", "meta": {"error": "Creation was interrupted"}})
            raise
        except (WorkspaceError, FetchError, OSError) as e:
            log.warning("workspace %s creation failed: %s", wid, e)
            self.db.update_workspace(wid, {"status": "error", "meta": {"error": friendly_git_error(str(e))}})
        except Exception as e:  # noqa: BLE001
            log.exception("workspace %s creation failed", wid)
            self.db.update_workspace(wid, {"status": "error", "meta": {"error": f"{type(e).__name__}: {e}"}})

    def _materialise(self, repo: dict, work: Path) -> int:
        fs = WorkspaceFS(work)
        n = 0
        for f in self.db.repo_listing(repo["id"]):
            rec = self.db.repo_file(repo["id"], f["path"])
            if rec is None:
                continue
            _, target = fs._writable(f["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rec["text"], encoding="utf-8")
            n += 1
        return n

    async def delete(self, wid: str) -> None:
        async with self.lock(wid):
            self.db.delete_workspace(wid)
            await asyncio.to_thread(shutil.rmtree, self.root / wid, True)
        self._locks.pop(wid, None)

    # -- review ---------------------------------------------------------------
    async def changes(self, wid: str) -> dict:
        """Uncommitted changes against the last commit, plus commits not yet pushed."""
        async with self.lock(wid):
            idx = await self._snapshot_index(wid)
            try:
                numstat = await self.git(wid, "diff", "--cached", "-M", "--numstat", "-z", "HEAD", index=idx)
                names = await self.git(wid, "diff", "--cached", "-M", "--name-status", "-z", "HEAD", index=idx)
            finally:
                idx.unlink(missing_ok=True)
            head = (await self.git(wid, "rev-parse", "HEAD")).strip()
        stats = _parse_numstat(numstat)
        out: list[Change] = []
        parts = names.split("\0")
        i = 0
        while i < len(parts) - 1 and len(out) < MAX_CHANGED_LISTED:
            code = parts[i]
            if code.startswith("R"):
                old, new = parts[i + 1], parts[i + 2]
                i += 3
                a, r = stats.get(new, (None, None))
                out.append(Change(new, "R", a, r, old))
            else:
                path = parts[i + 1]
                i += 2
                a, r = stats.get(path, (None, None))
                out.append(Change(path, code[:1], a, r))
        ws = self.db.get_workspace(wid)
        return {"head": head, "base_sha": ws["base_sha"], "files": [c.__dict__ for c in out],
                "truncated": len(out) >= MAX_CHANGED_LISTED}

    async def diff(self, wid: str, path: str | None = None, binary: bool = False) -> str:
        async with self.lock(wid):
            idx = await self._snapshot_index(wid)
            try:
                args = ["diff", "--cached", "-M", "HEAD"]
                if binary:
                    args.insert(2, "--binary")
                if path:
                    args += ["--", self.fs(wid)._rel(path).as_posix()]
                return await self.git(wid, *args, index=idx, timeout=300)
            finally:
                idx.unlink(missing_ok=True)

    async def archive(self, wid: str) -> Path:
        """Zip of the working tree as it is now (git-ignored files such as node_modules excluded)."""
        ctl = self.dirs(wid)[2]
        async with self.lock(wid):
            idx = await self._snapshot_index(wid)
            try:
                tree = (await self.git(wid, "write-tree", index=idx)).strip()
            finally:
                idx.unlink(missing_ok=True)
            out = ctl / f"archive-{uuid.uuid4().hex[:8]}.zip"
            await self.git(wid, "archive", "--format=zip", "-o", str(out), tree, timeout=300)
        return out

    async def discard(self, wid: str, path: str | None = None) -> None:
        """Throws away uncommitted changes (one path, or everything). Untracked files are deleted;
        git-ignored files (dependencies, build output) are kept."""
        async with self.lock(wid):
            if path:
                rel = self.fs(wid)._rel(path).as_posix()
                tracked = await self.git(wid, "ls-tree", "--name-only", "HEAD", "--", rel)
                if tracked.strip():
                    await self.git(wid, "checkout", "HEAD", "--", rel)
                else:
                    await self.git(wid, "clean", "-fdq", "--", rel)
            else:
                await self.git(wid, "reset", "--hard", "--quiet", "HEAD")
                await self.git(wid, "clean", "-fdq")

    async def commit(self, wid: str, message: str, author: dict) -> str:
        message = message.strip() or "Changes from Qwen Desktop"
        async with self.lock(wid):
            await self.git(wid, "add", "-A", timeout=300)
            staged = await self.git(wid, "diff", "--cached", "--name-only", "HEAD")
            if not staged.strip():
                raise WorkspaceError("There are no changes to commit")
            await self.git(wid, "-c", f"user.name={author['display_name']}",
                           "-c", f"user.email={author['username']}@qwen-desktop.local",
                           "commit", "--quiet", "--no-verify", "-m", message)
            return (await self.git(wid, "rev-parse", "HEAD")).strip()

    async def push(self, wid: str, branch: str) -> dict:
        ws = self.db.get_workspace(wid)
        repo = self.db.get_repo(ws["repo_id"], private=True) if ws.get("repo_id") else None
        if not repo or repo["source"] != "git":
            raise WorkspaceError("Only workspaces cloned from a git repository can be pushed")
        if not _BRANCH.match(branch):
            raise WorkspaceError("Invalid branch name")
        async with self.lock(wid):
            sha = (await self.git(wid, "rev-parse", "HEAD")).strip()
            if sha == ws["base_sha"]:
                raise WorkspaceError("Nothing to push: commit your changes first")
            try:
                # Never forced: an existing branch only moves forward.
                await self.git(wid, "push", "--quiet", "--no-verify", repo["url"], f"HEAD:refs/heads/{branch}",
                               token=repo.get("auth_token"), timeout=300)
            except WorkspaceError as e:
                msg = str(e)
                if "non-fast-forward" in msg or "rejected" in msg or "fetch first" in msg:
                    raise WorkspaceError(f"Branch “{branch}” already has other commits; choose a new branch name") from None
                if "403" in msg or "denied" in msg.lower() or "could not read Username" in msg:
                    raise WorkspaceError("Push was refused. The repository's access token needs write access to its "
                                         "contents (update the token on the project's repository).") from None
                raise
        info = {"branch": branch, "sha": sha, "at": time.time(), "url": compare_url(repo["url"], branch, ws["base_ref"])}
        self.db.update_workspace(wid, {"meta": {**(ws["meta"] or {}), "pushed": info}})
        return info

    # -- sandbox --------------------------------------------------------------
    def runner(self, wid: str, inputs_dir: Path | None = None):
        """run(kind, script, timeout) for run_shell in this workspace: the tree at /work (read-write), the
        repository data at /gitdir (read-only, so `git status/diff/log` work but commits are left to the user)."""
        work, git, ctl = self.dirs(wid)

        async def run(kind: str, script: str, timeout: int) -> ToolResult:
            if not self.sandbox.available:
                return ToolResult("Error: the code sandbox is not available on this server")
            if kind != "shell":
                return ToolResult("Error: in a workspace, run Python through run_shell (python3 script.py)")
            started = time.time()
            res = await self.sandbox.exec(
                work=work, ctl=ctl, kind="shell", script=script, timeout=timeout, memory=SHELL_MEMORY,
                mounts=[(git, "/gitdir", "ro")] + ([(inputs_dir, "/inputs", "ro")] if inputs_dir else []),
                env={"GIT_DIR": "/gitdir", "GIT_WORK_TREE": "/work", "GIT_OPTIONAL_LOCKS": "0"})
            changed = await asyncio.to_thread(self.sandbox.changed_files, work, started, 40)
            status, failed = describe(res, timeout)
            parts = [clip(res.stdout) or "(no output)", *status]
            if changed:
                parts.append("Files written by this command: " + ", ".join(str(p.relative_to(work)) for p in changed))
            text = "\n\n".join(parts)
            if failed and not text.startswith("Error:"):
                text = "Error: " + text
            return ToolResult(text, {"workspace_changed": True} if changed else {})
        return run


def _parse_numstat(raw: str) -> dict[str, tuple[int | None, int | None]]:
    """`git diff --numstat -z` → {path: (added, removed)}; renames are keyed by the new path."""
    out: dict[str, tuple[int | None, int | None]] = {}
    parts = raw.split("\0")
    i = 0
    while i < len(parts):
        rec = parts[i]
        if not rec:
            i += 1
            continue
        fields = rec.split("\t")
        if len(fields) < 3:
            i += 1
            continue
        a = None if fields[0] == "-" else int(fields[0])
        r = None if fields[1] == "-" else int(fields[1])
        if fields[2] == "":            # rename: old and new paths follow as separate records
            out[parts[i + 2]] = (a, r)
            i += 3
        else:
            out[fields[2]] = (a, r)
            i += 1
    return out


def compare_url(repo_url: str, branch: str, base: str | None) -> str | None:
    """Where to open a pull/merge request for a pushed branch (GitHub, GitLab, Bitbucket)."""
    u = urlsplit(repo_url)
    path = u.path.removesuffix(".git").strip("/")
    host = (u.hostname or "").lower()
    if host == "github.com" or host.startswith("github."):
        return f"https://{u.netloc}/{path}/compare/{(base + '...') if base else ''}{branch}?expand=1"
    if "gitlab" in host:
        return f"https://{u.netloc}/{path}/-/merge_requests/new?merge_request%5Bsource_branch%5D={branch}"
    if "bitbucket" in host:
        return f"https://{u.netloc}/{path}/pull-requests/new?source={branch}"
    return None
