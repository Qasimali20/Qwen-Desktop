"""Code sandbox: runs model-written Python and shell commands in a locked-down Docker container.

Isolation per run: no network, read-only root FS, all capabilities dropped, no-new-privileges,
memory/CPU/PID limits, wall-clock timeout, host-uid (non-root). The image carries Python 3.12 with the
data stack, Node.js 22 + TypeScript, git and common CLI tools.

Two kinds of mounts:
- chat sandbox: each conversation has a persistent scratch workspace (files survive between calls, process
  state does not); the user's files are mounted read-only at ./data.
- project workspace (workspaces.py): a working copy mounted read-write at /work.
The script to run always comes from a separate read-only control directory mounted at /qd, so it never
appears among the user's files.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .db import Database, new_id
from .tools import ToolResult

log = logging.getLogger("qwen.sandbox")

IMAGE = "qwen-desktop-sandbox:3"
TIMEOUT_S = 120
MAX_TIMEOUT_S = 600
MAX_CONCURRENT = 4
MAX_STDOUT = 12_000
MAX_OUTPUT_FILES = 12
MAX_OUTPUT_BYTES = 50 * 2**20
_FIG_MARK = "__QD_FIGURES__ "
_SAFE = re.compile(r"[^\w.\- ()]+")
# Never offered to the user as "created files": dependency trees, VCS data, caches.
_NOISE_DIRS = {".qd", "data", ".git", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv",
               "venv", ".mypy_cache", "dist", "build", ".next", ".cache"}


@dataclass
class RunOutput:
    stdout: str
    exit_code: int
    timed_out: bool
    duration_s: float
    files: list[Path] = field(default_factory=list)


def clip(text: str, limit: int = MAX_STDOUT) -> str:
    """Keep the head and tail of long output: errors are usually at the end, context at the start."""
    if len(text) <= limit:
        return text
    return text[:limit // 2] + f"\n…[{len(text) - limit} characters truncated]…\n" + text[-limit // 2:]


def describe(res: RunOutput, timeout: int) -> tuple[list[str], bool]:
    """Status lines for the model and whether the run failed."""
    parts = []
    if res.timed_out:
        parts.append(f"Error: execution exceeded {timeout}s and was stopped.")
    elif res.exit_code:
        parts.append(f"Error: process exited with status {res.exit_code}.")
    return parts, res.timed_out or bool(res.exit_code)


class Sandbox:
    def __init__(self, db: Database, root: Path, uploads: Path):
        self.db = db
        self.root = root
        self.uploads = uploads
        self.root.mkdir(parents=True, exist_ok=True)
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        self.available = False

    async def probe(self) -> None:
        """Checks that Docker and the sandbox image are usable (called at start-up)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "image", "inspect", IMAGE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            self.available = await proc.wait() == 0
        except FileNotFoundError:
            self.available = False
        if not self.available:
            log.warning("code sandbox unavailable: build it with sandbox/build.sh (image %s)", IMAGE)

    # -- chat workspace -------------------------------------------------------
    def _dirs(self, cid: str) -> tuple[Path, Path, Path]:
        base = self.root / cid
        work, data, ctl = base / "work", base / "data", base / "ctl"
        for d in (work, data, ctl, work / "out"):
            d.mkdir(parents=True, exist_ok=True)
        return work, data, ctl

    def drop(self, cid: str) -> None:
        shutil.rmtree(self.root / cid, ignore_errors=True)

    @staticmethod
    def _safe_name(name: str) -> str:
        return _SAFE.sub("_", name).strip(" .") or "file"

    def _sync_inputs(self, data: Path, attachments: list[dict]) -> None:
        """Expose the user's files read-only under ./data (hard links: no copy, same inode)."""
        for a in attachments:
            src = Path(a["path"])
            if not src.is_file():
                continue
            dst = data / self._safe_name(a["filename"])
            if dst.exists() and dst.stat().st_size == src.stat().st_size:
                continue
            dst.unlink(missing_ok=True)
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)

    # -- execution ------------------------------------------------------------
    async def exec(self, *, work: Path, ctl: Path, kind: str, script: str, data: Path | None = None,
                   cwd: str = "/work", timeout: int = TIMEOUT_S, memory: str = "2g",
                   mounts: list[tuple[Path, str, str]] | None = None, env: dict[str, str] | None = None) -> RunOutput:
        """Runs `script` (kind "python" or "shell") with `work` mounted read-write at /work.
        `mounts` adds (host path, container path, "ro"|"rw") volumes; `env` adds environment variables."""
        run_id = new_id()[:12]
        script_file = ctl / (f"{run_id}.py" if kind == "python" else f"{run_id}.sh")  # unique: calls may run in parallel
        script_file.write_text(script)
        name = f"qd-{run_id}"
        timeout = max(5, min(int(timeout), MAX_TIMEOUT_S))
        args = [
            "docker", "run", "--rm", "--name", name,
            "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,exec,size=1g",
            "--memory", memory, "--memory-swap", memory, "--cpus", "2", "--pids-limit", "512",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-e", f"QD_CWD={cwd}",
            "-v", f"{work}:/work:rw", "-v", f"{ctl}:/qd:ro",
        ]
        if data is not None:
            args += ["-v", f"{data}:/work/data:ro"]
        for host, target, mode in mounts or ():
            args += ["-v", f"{host}:{target}:{mode}"]
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]
        args += ["-w", "/work"]
        if kind == "shell":
            args += ["--entrypoint", "/opt/shell.sh"]
        args += [IMAGE, f"/qd/{script_file.name}"]

        try:
            async with self._sem:
                started = time.time()
                proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                            stderr=asyncio.subprocess.STDOUT)
                timed_out = False
                try:
                    raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except (asyncio.TimeoutError, asyncio.CancelledError) as e:
                    timed_out = isinstance(e, asyncio.TimeoutError)
                    await (await asyncio.create_subprocess_exec(
                        "docker", "kill", name, stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL)).wait()
                    if not timed_out:
                        raise
                    raw, _ = await proc.communicate()
        finally:
            script_file.unlink(missing_ok=True)
        out = raw.decode(errors="replace")
        if kind == "python" and (i := out.rfind(_FIG_MARK)) >= 0:
            out = out[:i].rstrip()
        return RunOutput(out, proc.returncode or 0, timed_out, round(time.time() - started, 2))

    @staticmethod
    def changed_files(work: Path, since: float, limit: int = MAX_OUTPUT_FILES) -> list[Path]:
        files = []
        for dirpath, dirnames, filenames in os.walk(work):
            dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS and not d.startswith(".")]
            for fn in filenames:
                p = Path(dirpath) / fn
                if fn.startswith(".") or not p.is_file():
                    continue
                st = p.stat()
                if st.st_mtime >= since - 1 and st.st_size <= MAX_OUTPUT_BYTES:
                    files.append(p)
        return sorted(files, key=lambda p: (p.suffix != ".png", str(p)))[:limit]

    def _register(self, files: list[Path], work: Path, user_id: str) -> list[dict]:
        """Copy produced files into managed storage as attachments the user can view/download."""
        outs = []
        for p in files:
            mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            kind = "image" if mime in ("image/png", "image/jpeg", "image/svg+xml", "image/gif", "image/webp") else "document"
            aid = new_id()
            dst = self.uploads / f"{aid}{p.suffix.lower()[:10]}"
            shutil.copy2(p, dst)
            meta = {"generated": True, "workspace_path": str(p.relative_to(work))}
            att = self.db.add_attachment({"id": aid, "user_id": user_id, "filename": p.name, "mime": mime,
                                          "kind": kind, "size": dst.stat().st_size, "path": str(dst), "meta": meta})
            outs.append({"id": att["id"], "filename": att["filename"], "mime": mime, "kind": kind,
                         "size": att["size"], "path": meta["workspace_path"]})
        return outs

    def prepare_inputs(self, cid: str, inputs: list[dict]) -> Path:
        """The chat's input files (hard links), ready to mount read-only; used by workspace runs at /inputs."""
        data = self._dirs(cid)[1]
        self._sync_inputs(data, inputs)
        return data

    def runner(self, conv: dict, inputs: list[dict]):
        """Returns run(kind, script) bound to one conversation's scratch workspace."""
        async def run(kind: str, script: str, timeout: int = TIMEOUT_S) -> ToolResult:
            if not self.available:
                return ToolResult("Error: the code sandbox is not available on this server")
            work, data, ctl = self._dirs(conv["id"])
            await asyncio.to_thread(self._sync_inputs, data, inputs)
            started = time.time()
            res = await self.exec(work=work, ctl=ctl, kind=kind, script=script, data=data, timeout=timeout)
            files = await asyncio.to_thread(self.changed_files, work, started)
            outs = await asyncio.to_thread(self._register, files, work, conv["user_id"])

            status, failed = describe(res, timeout)
            parts = [clip(res.stdout) or "(no output)", *status]
            if outs:
                parts.append("Files created (shown to the user): " + ", ".join(o["path"] for o in outs))
            result = "\n\n".join(parts)
            if failed and not result.startswith("Error:"):
                result = "Error: " + result  # mark failure for the UI while keeping the traceback
            return ToolResult(result, {"outputs": outs})
        return run

    async def cleanup_orphans(self) -> None:
        """Kills sandbox containers left running by a previous server process (crash or hard restart)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "ps", "-q", "--filter", f"ancestor={IMAGE}", "--filter", "name=qd-",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await proc.communicate()
        except FileNotFoundError:
            return
        ids = out.decode().split()
        # Only containers older than the longest allowed run: the sibling http/https process may own young ones.
        for cid in ids:
            p = await asyncio.create_subprocess_exec("docker", "inspect", "-f", "{{.State.StartedAt}}", cid,
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            started, _ = await p.communicate()
            try:
                t = datetime.fromisoformat(started.decode().strip()[:26].rstrip("Z") + "+00:00").timestamp()
            except ValueError:
                continue
            if time.time() - t > MAX_TIMEOUT_S + 60:
                await (await asyncio.create_subprocess_exec("docker", "kill", cid, stdout=asyncio.subprocess.DEVNULL,
                                                            stderr=asyncio.subprocess.DEVNULL)).wait()
