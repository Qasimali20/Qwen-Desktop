"""Admin health report: is every part of the deployment working right now?

Each check returns ``{id, group, label, status, detail, ms?}`` with status ``ok | warn | error``. Checks
run concurrently and are individually time-boxed, so one hung service never blocks the page. Nothing here
returns secrets: URLs are shown as host:port, never with credentials, and only to admins.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import ssl
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from .sandbox import IMAGE

if TYPE_CHECKING:
    from .state import State

PROBE_TIMEOUT_S = 3.0
CHECK_TIMEOUT_S = 8.0
BACKUP_DIR = Path(os.environ.get("QWEN_DESKTOP_BACKUP_DIR", "/home/tmc/qwen-desktop-backups/nightly"))
BACKUP_WARN_H, BACKUP_ERROR_H = 26, 50
DISK_WARN, DISK_ERROR = 0.15, 0.05          # free fraction
CERT_WARN_DAYS = 30
FAILED_LOGINS_WARN = 20                     # in 24 h
STARTED_AT = time.time()
ROOT = Path(__file__).resolve().parents[2]

Check = dict[str, Any]


def _check(cid: str, group: str, label: str, status: str, detail: str, ms: float | None = None) -> Check:
    c = {"id": cid, "group": group, "label": label, "status": status, "detail": detail}
    if ms is not None:
        c["ms"] = round(ms)
    return c


def _host(url: str) -> str:
    u = urlsplit(url)
    return u.hostname + (f":{u.port}" if u.port else "") if u.hostname else url


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _ago(seconds: float) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    if seconds < 172_800:
        return f"{seconds / 3600:.0f} h ago"
    return f"{seconds / 86_400:.0f} days ago"


# ---------------------------------------------------------------- model servers
async def _probe(http: httpx.AsyncClient, base_url: str, model: str) -> tuple[str, str, float]:
    """GET /models on an OpenAI-compatible server; checks the configured model is the one served."""
    t = time.perf_counter()
    try:
        r = await http.get(f"{base_url.rstrip('/')}/models", timeout=PROBE_TIMEOUT_S)
    except httpx.TimeoutException:
        return "error", f"{_host(base_url)}: no answer in {PROBE_TIMEOUT_S:.0f} s", (time.perf_counter() - t) * 1000
    except httpx.HTTPError as e:
        return "error", f"{_host(base_url)}: cannot connect ({type(e).__name__})", (time.perf_counter() - t) * 1000
    ms = (time.perf_counter() - t) * 1000
    if r.status_code != 200:
        return "error", f"{_host(base_url)}: HTTP {r.status_code}", ms
    try:
        served = [m.get("id") for m in r.json().get("data", [])]
    except ValueError:
        served = []
    if served and model not in served:
        return "warn", f"{_host(base_url)}: up, but serves {', '.join(map(str, served[:3]))} instead of {model}", ms
    return "ok", f"{_host(base_url)}", ms


async def _chat_models(s: State) -> list[Check]:
    out = []
    jobs = [(m, url) for m in s.settings.chat_models for url in m.replicas]
    results = await asyncio.gather(*(_probe(s.llm.http, url, m.model) for m, url in jobs))
    for m in s.settings.chat_models:
        rs = [(url, r) for (mm, url), r in zip(jobs, results) if mm.id == m.id]
        up = sum(r[0] != "error" for _, r in rs)
        status = "ok" if up == len(rs) and all(r[0] == "ok" for _, r in rs) else ("error" if up == 0 else "warn")
        lead = f"{up}/{len(rs)} replicas up" if len(rs) > 1 else ("up" if up else "down")
        detail = lead + " · " + " · ".join(r[1] for _, r in rs)
        fastest = min((r[2] for _, r in rs if r[0] != "error"), default=None)
        out.append(_check(f"model:{m.id}", "Models", m.label, status, detail, fastest))
    return out


async def _services(s: State) -> list[Check]:
    st = s.settings
    targets: list[tuple[str, str, str, str]] = []
    if st.embedding:
        targets.append(("svc:embedding", "Embeddings (knowledge search)", st.embedding.base_url, st.embedding.model))
    if st.ocr:
        targets.append(("svc:ocr", "OCR (scanned PDFs, images)", st.ocr.base_url, st.ocr.model))
    if st.asr:
        targets.append(("svc:asr", "Speech to text", st.asr.base_url, st.asr.model))
    targets += [(f"svc:tts:{v.id}", f"Voice · {v.label}", v.base_url, v.model) for v in st.tts_voices]
    results = await asyncio.gather(*(_probe(s.llm.http, url, model) for _, _, url, model in targets))
    return [_check(cid, "Services", label, st_, det, ms)
            for (cid, label, _, _), (st_, det, ms) in zip(targets, results)]


# ---------------------------------------------------------------- server
async def _sandbox(_s: State) -> list[Check]:
    t = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", "-f", "{{.Created}}", IMAGE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
    except FileNotFoundError:
        return [_check("sandbox", "Server", "Code sandbox", "error", "Docker is not installed")]
    ms = (time.perf_counter() - t) * 1000
    if proc.returncode != 0:
        msg = err.decode(errors="replace").strip().splitlines()[-1:] or ["unknown error"]
        hint = "image missing: run sandbox/build.sh" if "No such image" in msg[0] else msg[0][:200]
        return [_check("sandbox", "Server", "Code sandbox", "error", hint, ms)]
    running = await asyncio.create_subprocess_exec(
        "docker", "ps", "-q", "--filter", f"ancestor={IMAGE}", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    n = len((await running.communicate())[0].split())
    return [_check("sandbox", "Server", "Code sandbox", "ok",
                   f"Docker image {IMAGE} ready · {n} run{'s' if n != 1 else ''} in progress", ms)]


def _database(s: State) -> list[Check]:
    db = s.settings.data_dir / "qwen-desktop.db"
    wal = db.with_name(db.name + "-wal")
    t = time.perf_counter()
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            result = c.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            c.close()
    except sqlite3.Error as e:
        return [_check("db", "Server", "Database", "error", f"cannot open: {e}")]
    ms = (time.perf_counter() - t) * 1000
    size = db.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    if result != "ok":
        return [_check("db", "Server", "Database", "error", f"integrity problem: {result[:200]}", ms)]
    return [_check("db", "Server", "Database", "ok", f"integrity ok · {_size(size)}", ms)]


def _disk(s: State) -> list[Check]:
    u = shutil.disk_usage(s.settings.data_dir)
    free = u.free / u.total
    status = "error" if free < DISK_ERROR else "warn" if free < DISK_WARN else "ok"
    return [_check("disk", "Server", "Disk space", status,
                   f"{_size(u.free)} free of {_size(u.total)} ({free:.0%})")]


def _backups(_s: State) -> list[Check]:
    archives = sorted(BACKUP_DIR.glob("qwen-desktop-*.tgz"), key=lambda p: p.stat().st_mtime) if BACKUP_DIR.is_dir() else []
    if not archives:
        return [_check("backup", "Server", "Nightly backup", "error",
                       f"no backups in {BACKUP_DIR}: check the crontab entry for scripts/backup.sh")]
    last = archives[-1]
    age = time.time() - last.stat().st_mtime
    status = "error" if age > BACKUP_ERROR_H * 3600 else "warn" if age > BACKUP_WARN_H * 3600 else "ok"
    detail = f"last {_ago(age)} ({_size(last.stat().st_size)}) · {len(archives)} kept"
    if status != "ok":
        detail += f" · see {BACKUP_DIR / 'backup.log'}"
    return [_check("backup", "Server", "Nightly backup", status, detail)]


def _certificate(s: State) -> list[Check]:
    cert = s.settings.data_dir / "certs" / "cert.pem"
    if not cert.exists():
        return [_check("cert", "Server", "HTTPS certificate", "warn", "no certificate: HTTPS (and the mic) is off")]
    try:
        info = ssl._ssl._test_decode_cert(str(cert))  # type: ignore[attr-defined]  # stdlib, no extra dependency
        end = datetime.strptime(info["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
    except (ssl.SSLError, KeyError, ValueError, OSError) as e:
        return [_check("cert", "Server", "HTTPS certificate", "warn", f"cannot read: {e}")]
    days = (end - datetime.now(UTC)).days
    status = "error" if days < 0 else "warn" if days < CERT_WARN_DAYS else "ok"
    detail = f"expired {-days} days ago" if days < 0 else f"valid until {end:%d %b %Y} ({days} days)"
    return [_check("cert", "Server", "HTTPS certificate", status, detail + " · self-signed")]


def _process(_s: State) -> list[Check]:
    try:
        rev = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%h %cs"], capture_output=True,
                             text=True, timeout=3, check=False).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=3, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        rev, dirty = "", ""
    version = f"version {rev}" + (" + uncommitted changes" if dirty else "") if rev else "version unknown"
    return [_check("server", "Server", "App server", "ok", f"up since {_ago(time.time() - STARTED_AT)} · {version}")]


# ---------------------------------------------------------------- integrations & security
def _connectors(s: State) -> list[Check]:
    out = []
    for c in s.db.list_mcp_servers():
        if not c["enabled"]:
            out.append(_check(f"mcp:{c['id']}", "Connectors", c["label"], "ok", "switched off by an admin"))
            continue
        checked = f"checked {_ago(time.time() - c['checked_at'])}" if c["checked_at"] else "never checked"
        if c["status"] == "error":
            out.append(_check(f"mcp:{c['id']}", "Connectors", c["label"], "warn",
                              f"{c['error'] or 'error'} · {checked} (Refresh it under Connectors)"))
        else:
            out.append(_check(f"mcp:{c['id']}", "Connectors", c["label"], "ok",
                              f"{len(c['tool_list'] or [])} tools · {checked}"))
    return out


def _repo_sync(s: State) -> list[Check]:
    minutes = s.settings.repo_sync.interval_minutes
    repos = s.db.git_repo_overview()
    if not repos:
        return []
    if not minutes or not s.repos.enabled:
        return [_check("repo_sync", "Connectors", "Repository auto-sync", "ok",
                       f"off (repo_sync.interval_minutes is 0) · {len(repos)} git repositories sync on request")]
    on = [r for r in repos if r["auto_sync"]]
    failing = [r for r in on if (r["meta"] or {}).get("auto_sync_error")]
    # Checks claim a repo every interval; three missed intervals means the loop is not running.
    stale = [r for r in on if r["status"] in ("ready", "error") and r["checked_at"]
             and time.time() - r["checked_at"] > 3 * minutes * 60 + 120]
    detail = f"{len(on)}/{len(repos)} git repositories checked every {minutes} min"
    if failing:
        names = ", ".join(f"{r['project']}/{r['name']}" for r in failing[:3])
        return [_check("repo_sync", "Connectors", "Repository auto-sync", "warn",
                       f"{detail} · {len(failing)} can't be checked: {names} (see the project's Code section)")]
    if stale:
        return [_check("repo_sync", "Connectors", "Repository auto-sync", "warn",
                       f"{detail} · {len(stale)} not checked for over {3 * minutes} min: is the server's sync loop running?")]
    return [_check("repo_sync", "Connectors", "Repository auto-sync", "ok", detail)]


def _search(s: State) -> list[Check]:
    ws = s.settings.web_search
    if ws.provider == "wikipedia":
        return [_check("search", "Connectors", "Web search", "ok",
                       "Wikipedia only (add a Brave or Tavily key in config.yaml for full web search)")]
    if ws.provider in ("brave", "tavily") and not ws.api_key:
        return [_check("search", "Connectors", "Web search", "error",
                       f"{ws.provider}: environment variable {ws.api_key_env or '(unset)'} is empty")]
    return [_check("search", "Connectors", "Web search", "ok", ws.provider)]


def _security(s: State) -> list[Check]:
    sec = s.db.security_summary(time.time() - 86_400)
    n, ips, admins = sec["failed_logins"], sec["failed_ips"], sec["admins"]
    status = "warn" if n >= FAILED_LOGINS_WARN else "ok"
    detail = (f"{n} failed sign-in{'s' if n != 1 else ''} in 24 h from {ips} address{'es' if ips != 1 else ''}"
              if n else "no failed sign-ins in 24 h")
    return [_check("logins", "Security", "Sign-ins", status, detail + " (details under Activity)"),
            _check("admins", "Security", "Admin accounts", "ok" if admins <= 3 else "warn",
                   f"{admins} active admin{'s' if admins != 1 else ''}")]


# ---------------------------------------------------------------- report
async def report(s: State) -> dict:
    async def guarded(name: str, fn) -> list[Check]:
        try:
            if asyncio.iscoroutinefunction(fn):
                return await asyncio.wait_for(fn(s), CHECK_TIMEOUT_S)
            return await asyncio.wait_for(asyncio.to_thread(fn, s), CHECK_TIMEOUT_S)
        except TimeoutError:
            return [_check(f"fail:{name}", "Server", name, "error", f"check timed out after {CHECK_TIMEOUT_S:.0f} s")]
        except Exception as e:  # noqa: BLE001 — a broken check must not hide the others
            return [_check(f"fail:{name}", "Server", name, "error", f"check failed: {type(e).__name__}: {e}"[:300])]

    checks = [_chat_models, _services, _sandbox, _database, _disk, _backups, _certificate, _process,
              _connectors, _repo_sync, _search, _security]
    t = time.perf_counter()
    groups = await asyncio.gather(*(guarded(fn.__name__.strip("_").title(), fn) for fn in checks))
    items = [c for g in groups for c in g]
    worst = "error" if any(c["status"] == "error" for c in items) else (
        "warn" if any(c["status"] == "warn" for c in items) else "ok")
    return {"status": worst, "checked_at": time.time(), "ms": round((time.perf_counter() - t) * 1000), "checks": items}
