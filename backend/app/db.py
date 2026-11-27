"""SQLite persistence (WAL mode).

One connection guarded by a re-entrant lock: every statement is sub-millisecond on
local disk, so calling it from async handlers is cheaper than a thread-pool hop.
All user data is owned by a user_id; ownership is enforced at the query level.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .secretbox import SecretBox

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_login_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    ip TEXT,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New chat',
    model_id TEXT,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    system_prompt TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_conv_updated ON conversations(updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_conv_project ON conversations(project_id);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL DEFAULT '',
    blocks TEXT NOT NULL DEFAULT '[]',
    attachment_ids TEXT NOT NULL DEFAULT '[]',
    model_id TEXT,
    usage TEXT,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_msg_seq ON messages(conversation_id, seq);
CREATE INDEX IF NOT EXISTS ix_msg_created ON messages(created_at);
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    mime TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('image', 'document')),
    size INTEGER NOT NULL,
    path TEXT NOT NULL,
    text TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_att_project ON attachments(project_id);
CREATE TABLE IF NOT EXISTS user_settings (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

-- Knowledge base: chunked project documents with vectors + full-text index.
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    attachment_id TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    page INTEGER,
    text TEXT NOT NULL,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS ix_chunks_project ON chunks(project_id);
CREATE INDEX IF NOT EXISTS ix_chunks_att ON chunks(attachment_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Prompt shortcuts (/commands). owner_id NULL = team-wide (managed by admins).
CREATE TABLE IF NOT EXISTS prompts (
    id TEXT PRIMARY KEY,
    owner_id TEXT REFERENCES users(id) ON DELETE CASCADE,
    command TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_prompts_cmd ON prompts(COALESCE(owner_id, ''), command);

-- Personal API keys for the OpenAI/Anthropic-compatible gateway. Only a SHA-256 of the key is stored.
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    last_used_at REAL,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS ix_keys_user ON api_keys(user_id);
CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY,
    key_id TEXT REFERENCES api_keys(id) ON DELETE SET NULL,
    user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    model_id TEXT,
    endpoint TEXT NOT NULL,
    status INTEGER NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    duration_s REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_api_usage_created ON api_usage(created_at);

-- Code repositories attached to a project (zip upload or git clone), indexed for code-aware chat.
CREATE TABLE IF NOT EXISTS repos (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('zip', 'git')),
    url TEXT,
    ref TEXT,
    auth_token TEXT,                 -- git access token, kept server-side for sync; never returned by the API
    commit_sha TEXT,
    status TEXT NOT NULL CHECK (status IN ('queued', 'fetching', 'indexing', 'ready', 'error')),
    meta TEXT NOT NULL DEFAULT '{}',
    added_by TEXT,
    lease_owner TEXT,                -- process running the index job (http and https servers share the DB)
    lease_until REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (project_id, name)
);
CREATE TABLE IF NOT EXISTS repo_files (
    id INTEGER PRIMARY KEY,
    repo_id TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    lang TEXT,
    size INTEGER NOT NULL,
    lines INTEGER NOT NULL,
    sha TEXT NOT NULL,
    indexed_sha TEXT,                -- sha whose chunks are stored; differs from sha → needs (re)indexing
    text TEXT NOT NULL,
    UNIQUE (repo_id, path)
);
CREATE TABLE IF NOT EXISTS code_chunks (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES repo_files(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    path TEXT NOT NULL,              -- "<repo>/<path>", denormalised for search and citations
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS ix_code_chunks_project ON code_chunks(project_id);
CREATE INDEX IF NOT EXISTS ix_code_chunks_file ON code_chunks(file_id);
CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
    path, text, content='code_chunks', content_rowid='id', tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS code_chunks_ai AFTER INSERT ON code_chunks BEGIN
    INSERT INTO code_fts(rowid, path, text) VALUES (new.id, new.path, new.text);
END;
CREATE TRIGGER IF NOT EXISTS code_chunks_ad AFTER DELETE ON code_chunks BEGIN
    INSERT INTO code_fts(code_fts, rowid, path, text) VALUES ('delete', old.id, old.path, old.text);
END;

-- Read-only share links: a snapshot of a conversation as it was when shared (later edits are not visible
-- until the owner updates the link). One link per conversation; the id is the unguessable link token.
CREATE TABLE IF NOT EXISTS shares (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL UNIQUE REFERENCES conversations(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    messages TEXT NOT NULL,          -- JSON snapshot of the shared messages
    views INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
-- Files shown in a snapshot: readable through the link, and kept while any link uses them.
CREATE TABLE IF NOT EXISTS share_attachments (
    share_id TEXT NOT NULL REFERENCES shares(id) ON DELETE CASCADE,
    attachment_id TEXT NOT NULL,
    PRIMARY KEY (share_id, attachment_id)
);
CREATE INDEX IF NOT EXISTS ix_share_att ON share_attachments(attachment_id);

-- Project workspaces: a writable working copy on the server (cloned from a project repository, or empty)
-- that the model edits and runs commands in; the owner reviews the changes and downloads or pushes them.
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    repo_id TEXT REFERENCES repos(id) ON DELETE SET NULL,
    source TEXT NOT NULL CHECK (source IN ('repo', 'empty')),
    base_ref TEXT,                   -- branch the workspace started from
    base_sha TEXT,
    status TEXT NOT NULL CHECK (status IN ('creating', 'ready', 'error')),
    meta TEXT NOT NULL DEFAULT '{}', -- error, last push {branch, sha, url, at}
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (project_id, user_id, name)
);
CREATE INDEX IF NOT EXISTS ix_ws_project ON workspaces(project_id, user_id);

-- Skills: expert playbooks the model loads when a request matches (use_skill) or the user picks one.
-- owner_id NULL = team-wide (managed by admins). Built-in skills ship as files and are not stored here.
CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    owner_id TEXT REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_skills_name ON skills(COALESCE(owner_id, ''), name);

-- MCP servers (remote, streamable HTTP or SSE) added by admins. Their tools are offered to the model in
-- chats where the user switched the server on. headers hold credentials and are never returned by the API.
CREATE TABLE IF NOT EXISTS mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,        -- slug used in tool names: mcp__<name>__<tool>
    label TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    transport TEXT NOT NULL CHECK (transport IN ('http', 'sse')),
    headers TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    tool_list TEXT NOT NULL DEFAULT '[]',      -- cached tools/list: [{name, description, input_schema}]
    disabled_tools TEXT NOT NULL DEFAULT '[]', -- tool names hidden from the model
    status TEXT NOT NULL DEFAULT 'unknown' CHECK (status IN ('unknown', 'ok', 'error')),
    error TEXT,
    checked_at REAL,
    created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- Activity log: sign-ins, admin actions and team-wide changes. No foreign keys on purpose: entries outlive
-- the users they mention, so names are stored as they were at the time.
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    actor_id TEXT,
    actor_name TEXT NOT NULL,
    action TEXT NOT NULL,             -- e.g. user.deleted, auth.login_failed, connector.updated
    target_type TEXT,
    target_id TEXT,
    target_label TEXT,
    details TEXT NOT NULL DEFAULT '{}',
    ip TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log(at);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_log(action, at);
"""

# Columns added after the first release; applied idempotently on start-up.
MIGRATIONS = [
    ("projects", "user_id", "TEXT REFERENCES users(id) ON DELETE CASCADE"),
    ("conversations", "user_id", "TEXT REFERENCES users(id) ON DELETE CASCADE"),
    ("attachments", "user_id", "TEXT REFERENCES users(id) ON DELETE CASCADE"),
    # 'none' = private to the owner, 'view' = team can chat with it, 'edit' = team can also change it.
    ("projects", "team_access", "TEXT NOT NULL DEFAULT 'none'"),
    ("messages", "meta", "TEXT NOT NULL DEFAULT '{}'"),
    # Bumped whenever a project's searchable chunks change, so every server process (http and https share
    # the DB) can tell that its in-memory vector cache is stale.
    ("projects", "kb_rev", "INTEGER NOT NULL DEFAULT 0"),
    # The project workspace a chat works in (files it reads, writes and runs commands on).
    ("conversations", "workspace_id", "TEXT REFERENCES workspaces(id) ON DELETE SET NULL"),
    # Rolling summary of the turns that no longer fit the context (summary.py): JSON {text, upto_id, upto_seq, …}.
    ("conversations", "context_summary", "TEXT"),
    # Automatic sync of git repositories (RepoService._auto_sync_forever): per-repo switch, last remote check.
    ("repos", "auto_sync", "INTEGER NOT NULL DEFAULT 1"),
    ("repos", "checked_at", "REAL"),
]
TEAM_ACCESS = ("none", "view", "edit")
POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_conv_user ON conversations(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_proj_user ON projects(user_id);
CREATE INDEX IF NOT EXISTS ix_att_user ON attachments(user_id);
"""

_JSON_COLS = {"context_summary", "blocks", "attachment_ids", "usage", "meta", "messages", "headers", "tool_list", "disabled_tools", "details"}
ATT_PUBLIC_COLS = "id, user_id, project_id, filename, mime, kind, size, path, meta, created_at"


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> float:
    return time.time()


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    for k in _JSON_COLS & d.keys():
        if d[k] is not None:
            d[k] = json.loads(d[k])
    return d


class Database:
    def __init__(self, path: Path, box: SecretBox | None = None):
        """`box` encrypts stored credentials (repo tokens, MCP headers); without one they are kept as given."""
        self.box = box
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._c.row_factory = sqlite3.Row
        self._c.execute("PRAGMA journal_mode=WAL")
        self._c.execute("PRAGMA synchronous=NORMAL")
        self._c.execute("PRAGMA foreign_keys=ON")
        self._c.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, col, decl in MIGRATIONS:
            cols = {r[1] for r in self._c.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self._c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self._c.executescript(POST_MIGRATION_INDEXES)

    # -- credentials at rest (secretbox.py) --------------------------------------
    def _seal(self, value: str | None) -> str | None:
        return self.box.seal(value) if self.box else value

    def _open(self, value: str | None) -> str | None:
        return self.box.open(value) if self.box else value

    def _open_repo(self, r: dict | None) -> dict | None:
        if r and "auth_token" in r:
            r["auth_token"] = self._open(r["auth_token"])
        return r

    def _seal_headers(self, headers: dict | None) -> dict | str:
        """MCP headers are stored as a sealed JSON string; an empty dict stays a plain `{}`."""
        return self._seal(json.dumps(headers)) if headers and self.box else (headers or {})

    def _open_mcp(self, r: dict | None) -> dict | None:
        if r and isinstance(r.get("headers"), str):
            plain = self._open(r["headers"])
            r["headers"] = json.loads(plain) if plain else {}
        return r

    def encrypt_secrets(self) -> int:
        """Seal credentials stored before encryption existed (idempotent; safe with two processes starting at
        once: each row is only replaced if it still holds the plain value read). Returns rows converted."""
        if not self.box:
            return 0
        n = 0
        for r in self._all("SELECT id, auth_token FROM repos WHERE auth_token IS NOT NULL AND auth_token != '' "
                           "AND auth_token NOT LIKE 'enc:v1:%'"):
            n += self._exec("UPDATE repos SET auth_token = ? WHERE id = ? AND auth_token = ?",
                            (self._seal(r["auth_token"]), r["id"], r["auth_token"]))
        with self._lock:
            rows = self._c.execute("SELECT id, headers FROM mcp_servers WHERE headers IS NOT NULL "
                                   "AND headers NOT LIKE '\"enc:v1:%'").fetchall()
        for sid, raw in rows:
            headers = json.loads(raw) if raw else {}
            if isinstance(headers, dict) and headers:
                n += self._exec("UPDATE mcp_servers SET headers = ? WHERE id = ? AND headers = ?",
                                (json.dumps(self._seal_headers(headers)), sid, raw))
        return n

    def plaintext_secret_count(self) -> int:
        """Stored credentials that are not encrypted (for the health report)."""
        return self._one("SELECT (SELECT COUNT(*) FROM repos WHERE auth_token IS NOT NULL AND auth_token != '' "
                         "AND auth_token NOT LIKE 'enc:v1:%') + (SELECT COUNT(*) FROM mcp_servers WHERE headers "
                         "NOT IN ('{}', 'null', '') AND headers NOT LIKE '\"enc:v1:%') AS n")["n"]

    # -- primitives -----------------------------------------------------------
    def _all(self, sql: str, args: tuple | dict = ()) -> list[dict]:
        with self._lock:
            return [_row(r) for r in self._c.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple | dict = ()) -> dict | None:
        with self._lock:
            return _row(self._c.execute(sql, args).fetchone())

    def _exec(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._c.execute(sql, args).rowcount

    def _update(self, table: str, id_: str, fields: dict[str, Any]) -> None:
        if not fields:
            return
        fields = {k: json.dumps(v) if k in _JSON_COLS else v for k, v in fields.items()}
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE {table} SET {cols} WHERE id = ?", (*fields.values(), id_))

    # -- users & sessions -----------------------------------------------------
    USER_COLS = "id, username, display_name, role, disabled, created_at, last_login_at"

    def count_users(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM users")["n"]

    def create_user(self, username: str, display_name: str, password_hash: str, role: str = "user") -> dict:
        uid = new_id()
        self._exec("INSERT INTO users(id, username, display_name, password_hash, role, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?)", (uid, username, display_name, password_hash, role, now()))
        return self.get_user(uid)

    def get_user(self, uid: str) -> dict | None:
        return self._one(f"SELECT {self.USER_COLS} FROM users WHERE id = ?", (uid,))

    def get_user_auth(self, username: str) -> dict | None:
        return self._one("SELECT * FROM users WHERE username = ?", (username,))

    def password_hash(self, uid: str) -> str | None:
        r = self._one("SELECT password_hash FROM users WHERE id = ?", (uid,))
        return r["password_hash"] if r else None

    def update_user(self, uid: str, fields: dict) -> dict | None:
        self._update("users", uid, fields)
        return self.get_user(uid)

    def delete_user(self, uid: str) -> list[str]:
        paths = [r["path"] for r in self._all("SELECT path FROM attachments WHERE user_id = ?", (uid,))]
        with self._lock:
            # Other members' chats in this user's projects survive, detached from the project.
            self._c.execute("UPDATE conversations SET project_id = NULL WHERE user_id IS NOT ? AND project_id IN "
                            "(SELECT id FROM projects WHERE user_id = ?)", (uid, uid))
            # Projects/conversations cascade from users; attachments too. Explicit for clarity.
            self._c.execute("DELETE FROM conversations WHERE user_id = ?", (uid,))
            self._c.execute("DELETE FROM projects WHERE user_id = ?", (uid,))
            self._c.execute("DELETE FROM attachments WHERE user_id = ?", (uid,))
            self._c.execute("DELETE FROM users WHERE id = ?", (uid,))
        return paths

    def list_users(self) -> list[dict]:
        return self._all(
            f"SELECT {', '.join('u.' + c for c in self.USER_COLS.split(', '))}, "
            "(SELECT COUNT(*) FROM conversations c WHERE c.user_id = u.id) AS chat_count, "
            "(SELECT MAX(s.last_seen_at) FROM sessions s WHERE s.user_id = u.id) AS last_seen_at "
            "FROM users u ORDER BY u.created_at")

    def claim_orphans(self, uid: str) -> None:
        """Assign pre-auth data (created before user accounts existed) to `uid`."""
        with self._lock:
            for t in ("projects", "conversations", "attachments"):
                self._c.execute(f"UPDATE {t} SET user_id = ? WHERE user_id IS NULL", (uid,))
            legacy = self._c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
            if legacy:
                self._c.execute("INSERT OR IGNORE INTO user_settings(user_id, key, value) "
                                "SELECT ?, key, value FROM settings", (uid,))
                self._c.execute("DROP TABLE settings")

    def create_session(self, token_hash: str, uid: str, ttl_s: float, ip: str | None, ua: str | None) -> None:
        t = now()
        self._exec("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)", (token_hash, uid, t, t + ttl_s, t, ip, ua))
        self._exec("UPDATE users SET last_login_at = ? WHERE id = ?", (t, uid))

    def session_user(self, token_hash: str) -> dict | None:
        return self._one(
            f"SELECT {', '.join('u.' + c for c in self.USER_COLS.split(', '))}, s.last_seen_at AS session_seen "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0", (token_hash, now()))

    def touch_session(self, token_hash: str, ttl_s: float) -> None:
        t = now()
        self._exec("UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?", (t, t + ttl_s, token_hash))

    def delete_session(self, token_hash: str) -> None:
        self._exec("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_user_sessions(self, uid: str, keep: str | None = None) -> None:
        self._exec("DELETE FROM sessions WHERE user_id = ? AND token_hash IS NOT ?", (uid, keep))

    def purge_expired_sessions(self) -> None:
        self._exec("DELETE FROM sessions WHERE expires_at < ?", (now(),))

    # -- usage analytics ------------------------------------------------------
    def usage(self, since: float) -> dict[str, list[dict]]:
        """Token usage from chats in the app and from the API gateway, grouped several ways."""
        events = (
            "WITH ev AS ("
            " SELECT c.user_id, m.model_id, m.created_at AS ts, 'app' AS source,"
            "  COALESCE(json_extract(m.usage, '$.prompt_tokens'), 0) AS pt,"
            "  COALESCE(json_extract(m.usage, '$.completion_tokens'), 0) AS ct"
            " FROM messages m JOIN conversations c ON c.id = m.conversation_id"
            " WHERE m.role = 'assistant' AND m.created_at >= :since"
            " UNION ALL"
            " SELECT user_id, model_id, created_at, 'api', prompt_tokens, completion_tokens"
            " FROM api_usage WHERE created_at >= :since AND status < 400) ")
        tok = "COUNT(*) AS replies, SUM(pt) AS prompt_tokens, SUM(ct) AS completion_tokens"
        q = lambda sel, rest: self._all(f"{events} SELECT {sel}, {tok} FROM ev {rest}", {"since": since})
        return {
            "by_user": q("user_id", "WHERE user_id IS NOT NULL GROUP BY user_id"),
            "by_model": q("model_id", "GROUP BY model_id"),
            "by_source": q("source", "GROUP BY source"),
            "by_day": q("date(ts, 'unixepoch', 'localtime') AS day", "GROUP BY day ORDER BY day"),
        }

    # -- settings -------------------------------------------------------------
    def get_settings(self, uid: str) -> dict[str, Any]:
        return {r["key"]: json.loads(r["value"])
                for r in self._all("SELECT key, value FROM user_settings WHERE user_id = ?", (uid,))}

    def put_settings(self, uid: str, values: dict[str, Any]) -> None:
        with self._lock:
            self._c.executemany(
                "INSERT INTO user_settings(user_id, key, value) VALUES(?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                [(uid, k, json.dumps(v)) for k, v in values.items()])

    # -- projects -------------------------------------------------------------
    # A user sees their own projects plus every project shared with the team. Conversations are
    # always private: counts and listings only include the caller's own chats.
    _PROJECT_COLS = ("p.*, u.display_name AS owner_name, "
                     "(SELECT COUNT(*) FROM attachments a WHERE a.project_id = p.id) AS file_count")

    def list_projects(self, uid: str) -> list[dict]:
        return self._all(
            f"SELECT {self._PROJECT_COLS}, "
            "(SELECT COUNT(*) FROM conversations c WHERE c.project_id = p.id AND c.user_id = :uid) AS chat_count "
            "FROM projects p JOIN users u ON u.id = p.user_id "
            "WHERE p.user_id = :uid OR p.team_access != 'none' ORDER BY p.updated_at DESC", {"uid": uid})

    def get_project(self, pid: str, uid: str | None = None) -> dict | None:
        """Unfiltered (uid None) or the project as visible to `uid` (owner, or shared with the team)."""
        if uid is None:
            return self._one("SELECT * FROM projects WHERE id = ?", (pid,))
        return self._one(f"SELECT {self._PROJECT_COLS} FROM projects p JOIN users u ON u.id = p.user_id "
                         "WHERE p.id = ? AND (p.user_id = ? OR p.team_access != 'none')", (pid, uid))

    def owned_project_ids(self, uid: str) -> list[str]:
        return [r["id"] for r in self._all("SELECT id FROM projects WHERE user_id = ?", (uid,))]

    def create_project(self, uid: str, name: str, description: str = "", instructions: str = "",
                       team_access: str = "none") -> dict:
        pid, t = new_id(), now()
        self._exec("INSERT INTO projects(id, name, description, instructions, created_at, updated_at, user_id, "
                   "team_access) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (pid, name, description, instructions, t, t, uid, team_access))
        return self.get_project(pid, uid)

    def update_project(self, pid: str, fields: dict) -> None:
        self._update("projects", pid, {**fields, "updated_at": now()})

    def delete_project(self, pid: str) -> list[str]:
        """Deletes the project and its owner's chats in it; other members keep theirs, detached."""
        paths = [a["path"] for a in self.project_files(pid)]
        with self._lock:
            self._c.execute("UPDATE conversations SET project_id = NULL WHERE project_id = ? AND user_id IS NOT "
                            "(SELECT user_id FROM projects WHERE id = ?)", (pid, pid))
            self._c.execute("DELETE FROM projects WHERE id = ?", (pid,))
        return paths

    def project_files(self, pid: str, with_text: bool = False) -> list[dict]:
        cols = "*" if with_text else ATT_PUBLIC_COLS
        return self._all(f"SELECT {cols} FROM attachments WHERE project_id = ? ORDER BY created_at", (pid,))

    # -- conversations --------------------------------------------------------
    def list_conversations(self, uid: str, q: str | None = None, project_id: str | None = None,
                           limit: int = 200) -> list[dict]:
        where, args = ["c.user_id = ?"], [uid]
        if project_id:
            where.append("c.project_id = ?")
            args.append(project_id)
        if q:
            where.append("(c.title LIKE ? OR EXISTS (SELECT 1 FROM messages m "
                         "WHERE m.conversation_id = c.id AND m.content LIKE ?))")
            args += [f"%{q}%", f"%{q}%"]
        sql = ("SELECT c.id, c.title, c.model_id, c.project_id, c.pinned, c.created_at, c.updated_at "
               f"FROM conversations c WHERE {' AND '.join(where)} "
               "ORDER BY c.pinned DESC, c.updated_at DESC LIMIT ?")
        return self._all(sql, (*args, limit))

    def get_conversation(self, cid: str, uid: str | None = None) -> dict | None:
        if uid is None:
            return self._one("SELECT * FROM conversations WHERE id = ?", (cid,))
        return self._one("SELECT * FROM conversations WHERE id = ? AND user_id = ?", (cid, uid))

    def create_conversation(self, uid: str, model_id: str | None, project_id: str | None) -> dict:
        cid, t = new_id(), now()
        self._exec("INSERT INTO conversations(id, user_id, model_id, project_id, created_at, updated_at) "
                   "VALUES (?, ?, ?, ?, ?, ?)", (cid, uid, model_id, project_id, t, t))
        return self.get_conversation(cid)

    def update_conversation(self, cid: str, fields: dict, touch: bool = False) -> dict | None:
        if touch:
            fields = {**fields, "updated_at": now()}
        self._update("conversations", cid, fields)
        return self.get_conversation(cid)

    def delete_conversation(self, cid: str) -> list[str]:
        """Deletes the conversation; returns paths of attachments no longer referenced."""
        ids = self._attachment_ids_of(cid) | {r["attachment_id"] for r in self._all(
            "SELECT a.attachment_id FROM share_attachments a JOIN shares s ON s.id = a.share_id "
            "WHERE s.conversation_id = ?", (cid,))}
        self._exec("DELETE FROM conversations WHERE id = ?", (cid,))  # cascades to its share link
        return self._gc_attachments(ids)

    # -- messages -------------------------------------------------------------
    def messages(self, cid: str) -> list[dict]:
        return self._all("SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq", (cid,))

    def add_message(self, cid: str, role: str, content: str = "", *, blocks: list | None = None,
                    attachment_ids: list[str] | None = None, model_id: str | None = None,
                    usage: dict | None = None, mid: str | None = None, meta: dict | None = None) -> dict:
        mid = mid or new_id()
        with self._lock:
            seq = self._c.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE conversation_id = ?",
                                  (cid,)).fetchone()[0]
            self._c.execute(
                "INSERT INTO messages(id, conversation_id, seq, role, content, blocks, attachment_ids, model_id, "
                "usage, meta, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (mid, cid, seq, role, content, json.dumps(blocks or []), json.dumps(attachment_ids or []),
                 model_id, json.dumps(usage) if usage else None, json.dumps(meta or {}), now()))
            self._c.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now(), cid))
        return self._one("SELECT * FROM messages WHERE id = ?", (mid,))

    def get_message(self, mid: str) -> dict | None:
        return self._one("SELECT * FROM messages WHERE id = ?", (mid,))

    def truncate_from(self, cid: str, seq: int) -> list[str]:
        """Delete messages with seq >= `seq` (edit / regenerate). Returns orphaned file paths."""
        ids = {a for m in self._all("SELECT attachment_ids FROM messages WHERE conversation_id = ? AND seq >= ?",
                                    (cid, seq)) for a in m["attachment_ids"]}
        ids |= self._output_ids_from(cid, seq)
        with self._lock:
            self._c.execute("BEGIN")
            try:
                self._c.execute("DELETE FROM messages WHERE conversation_id = ? AND seq >= ?", (cid, seq))
                self._drop_stale_summary(cid, seq)
                self._c.execute("COMMIT")
            except BaseException:
                self._c.execute("ROLLBACK")
                raise
        return self._gc_attachments(ids)

    def _drop_stale_summary(self, cid: str, seq: int) -> None:
        """A summary covering messages from `seq` on describes turns that were just removed (edit / regenerate)."""
        self._c.execute("UPDATE conversations SET context_summary = NULL WHERE id = ? AND "
                        "json_extract(context_summary, '$.upto_seq') >= ?", (cid, seq))

    def set_context_summary(self, cid: str, summary: dict | None) -> None:
        self._update("conversations", cid, {"context_summary": summary})

    def replace_from(self, cid: str, seq: int, content: str, attachment_ids: list[str],
                     meta: dict | None = None) -> tuple[dict, list[str]]:
        """Edit: atomically drop messages with seq >= `seq` and insert a new user message at `seq`.

        Returns (new_message, orphaned_file_paths)."""
        mid = new_id()
        with self._lock:
            old = {a for m in self._all("SELECT attachment_ids FROM messages WHERE conversation_id = ? AND seq >= ?",
                                        (cid, seq)) for a in m["attachment_ids"]}
            old |= self._output_ids_from(cid, seq)
            self._c.execute("BEGIN")
            try:
                self._c.execute("DELETE FROM messages WHERE conversation_id = ? AND seq >= ?", (cid, seq))
                self._drop_stale_summary(cid, seq)
                self._c.execute(
                    "INSERT INTO messages(id, conversation_id, seq, role, content, attachment_ids, meta, created_at) "
                    "VALUES (?, ?, ?, 'user', ?, ?, ?, ?)",
                    (mid, cid, seq, content, json.dumps(attachment_ids), json.dumps(meta or {}), now()))
                self._c.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now(), cid))
                self._c.execute("COMMIT")
            except Exception:
                self._c.execute("ROLLBACK")
                raise
            return self.get_message(mid), self._gc_attachments(old - set(attachment_ids))

    # -- attachments ----------------------------------------------------------
    def add_attachment(self, att: dict) -> dict:
        att = {"project_id": None, "text": None, "meta": {}, "created_at": now(), **att}
        self._exec(
            "INSERT INTO attachments(id, user_id, project_id, filename, mime, kind, size, path, text, meta, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (att["id"], att["user_id"], att["project_id"], att["filename"], att["mime"], att["kind"], att["size"],
             att["path"], att["text"], json.dumps(att["meta"]), att["created_at"]))
        return self.get_attachment(att["id"], with_text=False)

    def get_attachment(self, aid: str, with_text: bool = True, uid: str | None = None) -> dict | None:
        cols = "*" if with_text else ATT_PUBLIC_COLS
        if uid is None:
            return self._one(f"SELECT {cols} FROM attachments WHERE id = ?", (aid,))
        return self._one(f"SELECT {cols} FROM attachments WHERE id = ? AND user_id = ?", (aid, uid))

    def get_attachments(self, ids: list[str], uid: str | None = None) -> dict[str, dict]:
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        sql = f"SELECT * FROM attachments WHERE id IN ({q})"
        args: tuple = tuple(ids)
        if uid is not None:
            sql += " AND user_id = ?"
            args += (uid,)
        return {a["id"]: a for a in self._all(sql, args)}

    def update_attachment(self, aid: str, fields: dict) -> None:
        self._update("attachments", aid, fields)

    def merge_attachment_meta(self, aid: str, patch: dict) -> None:
        self._exec("UPDATE attachments SET meta = json_patch(meta, ?) WHERE id = ?", (json.dumps(patch), aid))

    def delete_attachment(self, aid: str) -> str | None:
        a = self.get_attachment(aid, with_text=False)
        if a:
            self._exec("DELETE FROM attachments WHERE id = ?", (aid,))
            if a["project_id"]:
                self._exec(self._BUMP_KB, (a["project_id"],))
        return a["path"] if a else None

    def purge_unsent_uploads(self, older_than_s: float = 86_400) -> list[str]:
        """Chat uploads never sent (removed from the composer, tab closed). Tool outputs are kept
        while a message block references them."""
        rows = self._all(
            "SELECT id, path FROM attachments a WHERE a.project_id IS NULL AND a.created_at < ? "
            "AND NOT EXISTS (SELECT 1 FROM messages m, json_each(m.attachment_ids) j WHERE j.value = a.id) "
            "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.role = 'assistant' AND instr(m.blocks, a.id) > 0)",
            (now() - older_than_s,))
        for r in rows:
            self._exec("DELETE FROM attachments WHERE id = ?", (r["id"],))
        return [r["path"] for r in rows]

    def _attachment_ids_of(self, cid: str) -> set[str]:
        ids = {a for m in self._all("SELECT attachment_ids FROM messages WHERE conversation_id = ?", (cid,))
               for a in m["attachment_ids"]}
        return ids | self._output_ids_from(cid, 0)

    def _output_ids_from(self, cid: str, seq: int) -> set[str]:
        """Attachment ids produced by tools (e.g. analysis charts) in assistant blocks."""
        out: set[str] = set()
        for m in self._all("SELECT blocks FROM messages WHERE conversation_id = ? AND seq >= ? AND role = 'assistant'",
                           (cid, seq)):
            for b in m["blocks"]:
                out.update(o["id"] for o in b.get("outputs") or ())
        return out

    def _gc_attachments(self, ids: set[str]) -> list[str]:
        """Remove chat attachments (not project files) no longer referenced by any message."""
        paths = []
        for aid in ids:
            still_used = self._one(
                "SELECT 1 FROM messages, json_each(messages.attachment_ids) j WHERE j.value = ? "
                "UNION ALL SELECT 1 FROM messages WHERE role = 'assistant' AND instr(blocks, ?) > 0 "
                "UNION ALL SELECT 1 FROM share_attachments WHERE attachment_id = ? LIMIT 1",
                (aid, aid, aid))
            if not still_used:
                a = self.get_attachment(aid, with_text=False)
                if a and not a["project_id"]:
                    self._exec("DELETE FROM attachments WHERE id = ?", (aid,))
                    paths.append(a["path"])
        return paths

    # -- share links ----------------------------------------------------------
    SHARE_COLS = "id, conversation_id, user_id, title, views, created_at, updated_at"

    @staticmethod
    def _snapshot_attachment_ids(messages: list[dict]) -> set[str]:
        ids = {a for m in messages for a in m.get("attachment_ids") or ()}
        for m in messages:
            for b in m.get("blocks") or ():
                ids.update(o["id"] for o in b.get("outputs") or ())
        return ids

    def get_share(self, sid: str, with_messages: bool = False) -> dict | None:
        cols = "*" if with_messages else self.SHARE_COLS
        return self._one(f"SELECT {cols} FROM shares WHERE id = ?", (sid,))

    def share_of(self, cid: str) -> dict | None:
        return self._one(f"SELECT {self.SHARE_COLS}, json_array_length(messages) AS message_count "
                         "FROM shares WHERE conversation_id = ?", (cid,))

    def list_shares(self, uid: str) -> list[dict]:
        return self._all(f"SELECT {self.SHARE_COLS}, json_array_length(messages) AS message_count "
                         "FROM shares WHERE user_id = ? ORDER BY updated_at DESC", (uid,))

    def save_share(self, sid: str, cid: str, uid: str, title: str, messages: list[dict]) -> tuple[dict, list[str]]:
        """Creates or refreshes the conversation's link (keeping its token). Returns (share, orphaned paths)."""
        ids = self._snapshot_attachment_ids(messages)
        t = now()
        with self._lock:
            self._c.execute("BEGIN")
            try:
                old = self._c.execute("SELECT id FROM shares WHERE conversation_id = ?", (cid,)).fetchone()
                sid = old["id"] if old else sid
                before = {r[0] for r in self._c.execute(
                    "SELECT attachment_id FROM share_attachments WHERE share_id = ?", (sid,))}
                self._c.execute(
                    "INSERT INTO shares(id, conversation_id, user_id, title, messages, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(conversation_id) DO UPDATE SET "
                    "title = excluded.title, messages = excluded.messages, updated_at = excluded.updated_at",
                    (sid, cid, uid, title, json.dumps(messages), t, t))
                self._c.execute("DELETE FROM share_attachments WHERE share_id = ?", (sid,))
                self._c.executemany("INSERT INTO share_attachments(share_id, attachment_id) VALUES (?, ?)",
                                    [(sid, a) for a in ids])
                self._c.execute("COMMIT")
            except Exception:
                self._c.execute("ROLLBACK")
                raise
        return self.share_of(cid), self._gc_attachments(before - ids)

    def delete_share(self, cid: str) -> list[str]:
        """Revokes the conversation's link; returns paths of files only the snapshot still used."""
        share = self._one("SELECT id FROM shares WHERE conversation_id = ?", (cid,))
        if not share:
            return []
        ids = {r["attachment_id"] for r in self._all(
            "SELECT attachment_id FROM share_attachments WHERE share_id = ?", (share["id"],))}
        self._exec("DELETE FROM shares WHERE id = ?", (share["id"],))
        return self._gc_attachments(ids)

    def count_share_view(self, sid: str) -> None:
        self._exec("UPDATE shares SET views = views + 1 WHERE id = ?", (sid,))

    def shared_attachment(self, aid: str) -> bool:
        return self._one("SELECT 1 FROM share_attachments WHERE attachment_id = ? LIMIT 1", (aid,)) is not None

    # -- workspaces -----------------------------------------------------------
    def create_workspace(self, pid: str, uid: str, name: str, repo_id: str | None, base_ref: str | None) -> dict:
        """Creates a workspace record in status 'creating'; a numeric suffix keeps names unique per user."""
        wid, t = new_id(), now()
        base = name
        for i in range(1, 100):
            try:
                self._exec("INSERT INTO workspaces(id, project_id, user_id, name, repo_id, source, base_ref, status, "
                           "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'creating', ?, ?)",
                           (wid, pid, uid, name, repo_id, "repo" if repo_id else "empty", base_ref, t, t))
                return self.get_workspace(wid)
            except sqlite3.IntegrityError:
                name = f"{base}-{i + 1}"
        raise ValueError("Too many workspaces with this name")

    def get_workspace(self, wid: str) -> dict | None:
        return self._one("SELECT * FROM workspaces WHERE id = ?", (wid,))

    def list_workspaces(self, pid: str, uid: str) -> list[dict]:
        return self._all("SELECT w.*, (SELECT COUNT(*) FROM conversations c WHERE c.workspace_id = w.id) AS chats "
                         "FROM workspaces w WHERE w.project_id = ? AND w.user_id = ? ORDER BY w.updated_at DESC",
                         (pid, uid))

    def count_workspaces(self, uid: str) -> int:
        return self._one("SELECT COUNT(*) AS n FROM workspaces WHERE user_id = ?", (uid,))["n"]

    def update_workspace(self, wid: str, fields: dict) -> dict | None:
        self._update("workspaces", wid, {**fields, "updated_at": now()})
        return self.get_workspace(wid)

    def delete_workspace(self, wid: str) -> None:
        self._exec("DELETE FROM workspaces WHERE id = ?", (wid,))

    def stale_creating_workspaces(self, older_than: float) -> list[dict]:
        return self._all("SELECT * FROM workspaces WHERE status = 'creating' AND updated_at < ?", (older_than,))

    def all_project_workspaces(self, pid: str) -> list[str]:
        return [r["id"] for r in self._all("SELECT id FROM workspaces WHERE project_id = ?", (pid,))]

    def all_workspace_ids(self) -> set[str]:
        return {r["id"] for r in self._all("SELECT id FROM workspaces")}

    # -- knowledge chunks -----------------------------------------------------
    def replace_chunks(self, aid: str, pid: str, rows: list[tuple[int, int | None, str, bytes | None]]) -> None:
        with self._lock:
            self._c.execute("BEGIN")
            try:
                self._c.execute("DELETE FROM chunks WHERE attachment_id = ?", (aid,))
                self._c.executemany(
                    "INSERT INTO chunks(attachment_id, project_id, ord, page, text, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                    [(aid, pid, o, p, t, e) for o, p, t, e in rows])
                self._c.execute(self._BUMP_KB, (pid,))
                self._c.execute("COMMIT")
            except Exception:
                self._c.execute("ROLLBACK")
                raise

    def project_vectors(self, pid: str) -> list[tuple[int, bytes]]:
        with self._lock:
            return self._c.execute("SELECT id, embedding FROM chunks WHERE project_id = ? AND embedding IS NOT NULL "
                                   "ORDER BY id", (pid,)).fetchall()

    def fts_search(self, pid: str, match: str, limit: int) -> list[int]:
        with self._lock:
            try:
                rows = self._c.execute(
                    "SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
                    "WHERE chunks_fts MATCH ? AND c.project_id = ? ORDER BY bm25(chunks_fts) LIMIT ?",
                    (match, pid, limit)).fetchall()
            except sqlite3.OperationalError:
                return []
        return [r[0] for r in rows]

    def chunks_by_id(self, ids: list[int]) -> dict[int, dict]:
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        return {r["id"]: r for r in self._all(
            f"SELECT c.id, c.attachment_id, c.ord, c.page, c.text, a.filename FROM chunks c "
            f"JOIN attachments a ON a.id = c.attachment_id WHERE c.id IN ({q})", tuple(ids))}

    def project_chunk_count(self, pid: str) -> int:
        return self._one("SELECT COUNT(*) AS n FROM chunks WHERE project_id = ?", (pid,))["n"]


    # -- knowledge revision ---------------------------------------------------
    _BUMP_KB = "UPDATE projects SET kb_rev = kb_rev + 1 WHERE id = ?"

    def kb_rev(self, pid: str) -> int:
        r = self._one("SELECT kb_rev FROM projects WHERE id = ?", (pid,))
        return r["kb_rev"] if r else -1

    def bump_kb(self, pid: str) -> None:
        self._exec(self._BUMP_KB, (pid,))

    # -- code chunks ----------------------------------------------------------
    def code_vectors(self, pid: str) -> list[tuple[int, bytes]]:
        with self._lock:
            return self._c.execute("SELECT id, embedding FROM code_chunks WHERE project_id = ? AND embedding IS NOT "
                                   "NULL ORDER BY id", (pid,)).fetchall()

    def code_fts_search(self, pid: str, match: str, limit: int) -> list[int]:
        with self._lock:
            try:
                rows = self._c.execute(
                    "SELECT c.id FROM code_fts f JOIN code_chunks c ON c.id = f.rowid "
                    "WHERE code_fts MATCH ? AND c.project_id = ? ORDER BY bm25(code_fts, 2.0, 1.0) LIMIT ?",
                    (match, pid, limit)).fetchall()
            except sqlite3.OperationalError:
                return []
        return [r[0] for r in rows]

    def code_chunks_by_id(self, ids: list[int]) -> dict[int, dict]:
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        return {r["id"]: r for r in self._all(
            f"SELECT c.id, c.path, c.start_line, c.end_line, c.text, f.lang, f.repo_id FROM code_chunks c "
            f"JOIN repo_files f ON f.id = c.file_id WHERE c.id IN ({q})", tuple(ids))}

    # -- repositories ---------------------------------------------------------
    REPO_PUBLIC_COLS = ("id, project_id, name, source, url, ref, commit_sha, status, meta, added_by, auto_sync, "
                        "checked_at, created_at, updated_at")

    def create_repo(self, pid: str, name: str, source: str, added_by: str, url: str | None = None,
                    ref: str | None = None, auth_token: str | None = None) -> dict:
        """Creates the repo with a unique name in the project ("name", "name-2", ...)."""
        rid, t = new_id(), now()
        with self._lock:
            taken = {r[0] for r in self._c.execute("SELECT name FROM repos WHERE project_id = ?", (pid,))}
            unique, n = name, 2
            while unique in taken:
                unique, n = f"{name}-{n}", n + 1
            self._c.execute(
                "INSERT INTO repos(id, project_id, name, source, url, ref, auth_token, status, meta, added_by, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', '{}', ?, ?, ?)",
                (rid, pid, unique, source, url, ref, self._seal(auth_token), added_by, t, t))
        return self.get_repo(rid)

    def get_repo(self, rid: str, private: bool = False) -> dict | None:
        cols = "*" if private else self.REPO_PUBLIC_COLS
        return self._open_repo(self._one(f"SELECT {cols} FROM repos WHERE id = ?", (rid,)))

    def list_repos(self, pid: str) -> list[dict]:
        return self._all(f"SELECT {self.REPO_PUBLIC_COLS} FROM repos WHERE project_id = ? ORDER BY created_at", (pid,))

    def update_repo(self, rid: str, fields: dict) -> None:
        if "auth_token" in fields:
            fields = {**fields, "auth_token": self._seal(fields["auth_token"])}
        self._update("repos", rid, {**fields, "updated_at": now()})

    def merge_repo_meta(self, rid: str, patch: dict) -> None:
        self._exec("UPDATE repos SET meta = json_patch(meta, ?), updated_at = ? WHERE id = ?",
                   (json.dumps(patch), now(), rid))

    def delete_repo(self, rid: str) -> None:
        r = self.get_repo(rid)
        if r:
            self._exec("DELETE FROM repos WHERE id = ?", (rid,))
            self.bump_kb(r["project_id"])

    def claim_repo(self, rid: str, owner: str, ttl: float) -> bool:
        """Atomically take (or renew) the job lease. False if another live process holds it."""
        t = now()
        return self._exec("UPDATE repos SET lease_owner = ?, lease_until = ? WHERE id = ? AND "
                          "(lease_owner IS NULL OR lease_owner = ? OR lease_until < ?)",
                          (owner, t + ttl, rid, owner, t)) == 1

    def release_repo(self, rid: str, owner: str) -> None:
        self._exec("UPDATE repos SET lease_owner = NULL, lease_until = NULL WHERE id = ? AND lease_owner = ?",
                   (rid, owner))

    def unfinished_repos(self) -> list[str]:
        """Jobs that need a worker: not finished and with no live lease."""
        return [r["id"] for r in self._all(
            "SELECT id FROM repos WHERE status IN ('queued', 'fetching', 'indexing') "
            "AND (lease_until IS NULL OR lease_until < ?)", (now(),))]

    _CHECKABLE = "source = 'git' AND auto_sync = 1 AND status IN ('ready', 'error')"

    def repos_due_for_check(self, checked_before: float, limit: int = 50) -> list[dict]:
        """Git repos with auto-sync on whose remote was last checked before `checked_before` (oldest first)."""
        return [self._open_repo(r) for r in self._all(
            f"SELECT id, name, url, ref, auth_token, commit_sha, status, meta FROM repos "
            f"WHERE {self._CHECKABLE} AND (checked_at IS NULL OR checked_at < ?) "
            "ORDER BY COALESCE(checked_at, 0) LIMIT ?", (checked_before, limit))]

    def claim_repo_check(self, rid: str, checked_before: float) -> bool:
        """Atomically take the next remote check of a repo, so only one server process runs it per interval."""
        return self._exec(f"UPDATE repos SET checked_at = ? WHERE id = ? AND {self._CHECKABLE} "
                          "AND (checked_at IS NULL OR checked_at < ?)", (now(), rid, checked_before)) == 1

    def git_repo_overview(self) -> list[dict]:
        """Every git repo's auto-sync state, for the admin health report."""
        return self._all("SELECT r.name, p.name AS project, r.auto_sync, r.checked_at, r.status, r.meta FROM repos r "
                         "JOIN projects p ON p.id = r.project_id WHERE r.source = 'git'")

    def queue_repo_sync(self, rid: str) -> bool:
        """Queue a sync unless one is already queued or running (a manual sync may have just started)."""
        return self._exec("UPDATE repos SET status = 'queued', updated_at = ? WHERE id = ? "
                          "AND status IN ('ready', 'error')", (now(), rid)) == 1

    def sync_repo_files(self, rid: str, files: list[dict]) -> dict[str, int]:
        """Make repo_files match `files` (path, lang, size, lines, sha, text). Unchanged files keep their
        chunks; changed ones are marked for re-indexing; removed ones are deleted with their chunks."""
        with self._lock:
            existing = {r[0]: (r[1], r[2]) for r in self._c.execute(
                "SELECT path, id, sha FROM repo_files WHERE repo_id = ?", (rid,))}
            incoming = {f["path"]: f for f in files}
            added = [f for p, f in incoming.items() if p not in existing]
            changed = [f for p, f in incoming.items() if p in existing and existing[p][1] != f["sha"]]
            removed = [existing[p][0] for p in existing.keys() - incoming.keys()]
            self._c.execute("BEGIN")
            try:
                self._c.executemany(
                    "INSERT INTO repo_files(repo_id, path, lang, size, lines, sha, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(rid, f["path"], f["lang"], f["size"], f["lines"], f["sha"], f["text"]) for f in added])
                self._c.executemany(
                    "UPDATE repo_files SET lang = ?, size = ?, lines = ?, sha = ?, text = ? WHERE repo_id = ? AND path = ?",
                    [(f["lang"], f["size"], f["lines"], f["sha"], f["text"], rid, f["path"]) for f in changed])
                self._c.executemany("DELETE FROM repo_files WHERE id = ?", [(i,) for i in removed])
                self._c.execute("UPDATE projects SET kb_rev = kb_rev + 1 WHERE id = "
                                "(SELECT project_id FROM repos WHERE id = ?)", (rid,))
                self._c.execute("COMMIT")
            except Exception:
                self._c.execute("ROLLBACK")
                raise
        return {"added": len(added), "changed": len(changed), "removed": len(removed),
                "unchanged": len(incoming) - len(added) - len(changed)}

    def pending_repo_files(self, rid: str, limit: int) -> list[dict]:
        return self._all("SELECT id, path, lang, sha, text FROM repo_files WHERE repo_id = ? "
                         "AND indexed_sha IS NOT sha ORDER BY id LIMIT ?", (rid, limit))

    def replace_file_chunks(self, file_id: int, pid: str, sha: str,
                            rows: list[tuple[str, int, int, str, bytes]]) -> None:
        """Store a file's chunks (path, start, end, text, embedding) and mark it indexed at `sha`. If the file
        changed or vanished meanwhile, nothing is written (the next pass picks up the new version)."""
        with self._lock:
            self._c.execute("BEGIN")
            try:
                if not self._c.execute("SELECT 1 FROM repo_files WHERE id = ? AND sha = ?", (file_id, sha)).fetchone():
                    self._c.execute("ROLLBACK")
                    return
                self._c.execute("DELETE FROM code_chunks WHERE file_id = ?", (file_id,))
                self._c.executemany(
                    "INSERT INTO code_chunks(file_id, project_id, path, start_line, end_line, text, embedding) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", [(file_id, pid, *r) for r in rows])
                self._c.execute("UPDATE repo_files SET indexed_sha = ? WHERE id = ?", (sha, file_id))
                self._c.execute(self._BUMP_KB, (pid,))
                self._c.execute("COMMIT")
            except Exception:
                self._c.execute("ROLLBACK")
                raise

    def repo_stats(self, rid: str) -> dict:
        return self._one(
            "SELECT COUNT(*) AS files, COALESCE(SUM(indexed_sha IS sha), 0) AS indexed, COALESCE(SUM(size), 0) AS bytes, "
            "COALESCE(SUM(lines), 0) AS lines, (SELECT COUNT(*) FROM code_chunks c JOIN repo_files f2 "
            "ON f2.id = c.file_id WHERE f2.repo_id = :rid) AS chunks FROM repo_files WHERE repo_id = :rid", {"rid": rid})

    def repo_listing(self, rid: str) -> list[dict]:
        return self._all("SELECT path, lang, size, lines FROM repo_files WHERE repo_id = ? ORDER BY path", (rid,))

    def repo_file(self, rid: str, path: str) -> dict | None:
        return self._one("SELECT id, path, lang, size, lines, text FROM repo_files WHERE repo_id = ? AND path = ?",
                         (rid, path))

    def project_code_listing(self, pid: str) -> list[dict]:
        """Every indexed file in the project's repos as "<repo>/<path>" with size info (no text)."""
        return self._all("SELECT r.name || '/' || f.path AS path, f.lang, f.size, f.lines FROM repo_files f "
                         "JOIN repos r ON r.id = f.repo_id WHERE r.project_id = ? ORDER BY 1", (pid,))

    def project_code_file(self, pid: str, full_path: str) -> dict | None:
        return self._one("SELECT r.name || '/' || f.path AS path, f.lang, f.lines, f.text FROM repo_files f "
                         "JOIN repos r ON r.id = f.repo_id WHERE r.project_id = ? AND r.name || '/' || f.path = ?",
                         (pid, full_path))

    def iter_project_code(self, pid: str, batch: int = 200):
        """(full_path, text) for every file, fetched in batches so a large repo is never all in memory."""
        last = 0
        while True:
            rows = self._all("SELECT f.id, r.name || '/' || f.path AS path, f.text FROM repo_files f JOIN repos r "
                             "ON r.id = f.repo_id WHERE r.project_id = ? AND f.id > ? ORDER BY f.id LIMIT ?",
                             (pid, last, batch))
            if not rows:
                return
            for r in rows:
                yield r["path"], r["text"]
            last = rows[-1]["id"]

    # -- app meta -------------------------------------------------------------
    def meta_get(self, key: str) -> str | None:
        r = self._one("SELECT value FROM app_meta WHERE key = ?", (key,))
        return r["value"] if r else None

    def meta_set(self, key: str, value: str) -> None:
        self._exec("INSERT INTO app_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                   (key, value))

    # -- prompt shortcuts -----------------------------------------------------
    def list_prompts(self, uid: str) -> list[dict]:
        """Team prompts plus the user's own. A personal prompt shadows a team one with the same command."""
        return self._all("SELECT * FROM prompts WHERE owner_id IS NULL OR owner_id = ? "
                         "ORDER BY command, owner_id IS NULL", (uid,))

    def get_prompt(self, prompt_id: str, uid: str) -> dict | None:
        return self._one("SELECT * FROM prompts WHERE id = ? AND (owner_id IS NULL OR owner_id = ?)", (prompt_id, uid))

    def prompt_command_taken(self, owner_id: str | None, command: str, exclude: str | None = None) -> bool:
        return self._one("SELECT 1 FROM prompts WHERE owner_id IS ? AND command = ? AND id IS NOT ?",
                         (owner_id, command, exclude)) is not None

    def create_prompt(self, owner_id: str | None, command: str, title: str, description: str, body: str) -> dict:
        pid, t = new_id(), now()
        self._exec("INSERT INTO prompts(id, owner_id, command, title, description, body, created_at, updated_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (pid, owner_id, command, title, description, body, t, t))
        return self._one("SELECT * FROM prompts WHERE id = ?", (pid,))

    def update_prompt(self, prompt_id: str, fields: dict) -> dict:
        self._update("prompts", prompt_id, {**fields, "updated_at": now()})
        return self._one("SELECT * FROM prompts WHERE id = ?", (prompt_id,))

    def seed_team_prompt(self, command: str, title: str, description: str, body: str) -> None:
        """Insert unless the command exists. Safe when several server processes start at once."""
        t = now()
        self._exec("INSERT OR IGNORE INTO prompts(id, owner_id, command, title, description, body, created_at, "
                   "updated_at) VALUES (?, NULL, ?, ?, ?, ?, ?, ?)", (new_id(), command, title, description, body, t, t))

    def delete_prompt(self, prompt_id: str) -> None:
        self._exec("DELETE FROM prompts WHERE id = ?", (prompt_id,))

    # -- skills ---------------------------------------------------------------
    def list_skills(self, uid: str) -> list[dict]:
        """Team skills plus the user's own."""
        return self._all("SELECT * FROM skills WHERE owner_id IS NULL OR owner_id = ? ORDER BY name, owner_id IS NULL",
                         (uid,))

    def get_skill(self, skill_id: str, uid: str) -> dict | None:
        return self._one("SELECT * FROM skills WHERE id = ? AND (owner_id IS NULL OR owner_id = ?)", (skill_id, uid))

    def skill_name_taken(self, owner_id: str | None, name: str, exclude: str | None = None) -> bool:
        """Names are unique per owner, and a personal skill may not reuse a team skill's name (or vice versa)."""
        if owner_id is None:
            q, args = "SELECT 1 FROM skills WHERE name = ? AND id IS NOT ?", (name, exclude)
        else:
            q, args = ("SELECT 1 FROM skills WHERE name = ? AND id IS NOT ? AND (owner_id IS NULL OR owner_id = ?)",
                       (name, exclude, owner_id))
        return self._one(q, args) is not None

    def create_skill(self, owner_id: str | None, name: str, title: str, description: str, body: str) -> dict:
        sid, t = new_id(), now()
        self._exec("INSERT INTO skills(id, owner_id, name, title, description, body, created_at, updated_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (sid, owner_id, name, title, description, body, t, t))
        return self._one("SELECT * FROM skills WHERE id = ?", (sid,))

    def update_skill(self, skill_id: str, fields: dict) -> dict:
        self._update("skills", skill_id, {**fields, "updated_at": now()})
        return self._one("SELECT * FROM skills WHERE id = ?", (skill_id,))

    def delete_skill(self, skill_id: str) -> None:
        self._exec("DELETE FROM skills WHERE id = ?", (skill_id,))

    # -- MCP servers ------------------------------------------------------------
    def list_mcp_servers(self, enabled_only: bool = False) -> list[dict]:
        return [self._open_mcp(r) for r in self._all(
            "SELECT * FROM mcp_servers" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY label")]

    def get_mcp_server(self, sid: str) -> dict | None:
        return self._open_mcp(self._one("SELECT * FROM mcp_servers WHERE id = ?", (sid,)))

    def mcp_name_taken(self, name: str, exclude: str | None = None) -> bool:
        return self._one("SELECT 1 FROM mcp_servers WHERE name = ? AND id IS NOT ?", (name, exclude)) is not None

    def create_mcp_server(self, fields: dict, created_by: str) -> dict:
        sid, t = new_id(), now()
        row = {"id": sid, "created_by": created_by, "created_at": t, "updated_at": t, **fields}
        if "headers" in row:
            row["headers"] = self._seal_headers(row["headers"])
        row = {k: json.dumps(v) if k in _JSON_COLS else v for k, v in row.items()}
        self._exec(f"INSERT INTO mcp_servers({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        return self.get_mcp_server(sid)

    def update_mcp_server(self, sid: str, fields: dict, touch: bool = True) -> dict:
        if "headers" in fields:
            fields = {**fields, "headers": self._seal_headers(fields["headers"])}
        self._update("mcp_servers", sid, {**fields, **({"updated_at": now()} if touch else {})})
        return self.get_mcp_server(sid)

    def delete_mcp_server(self, sid: str) -> None:
        self._exec("DELETE FROM mcp_servers WHERE id = ?", (sid,))

    # -- activity log -----------------------------------------------------------
    def add_audit(self, entry: dict) -> None:
        row = {**entry, "at": now(), "details": json.dumps(entry.get("details") or {}, ensure_ascii=False)}
        self._exec(f"INSERT INTO audit_log({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))

    def list_audit(self, *, limit: int = 100, before: int | None = None, category: str | None = None,
                   q: str | None = None) -> list[dict]:
        where, args = [], []
        if before:
            where.append("id < ?"); args.append(before)
        if category:
            where.append("action LIKE ?"); args.append(f"{category}.%")
        if q:
            where.append("(actor_name LIKE ? OR target_label LIKE ? OR ip LIKE ?)"); args += [f"%{q}%"] * 3
        sql = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        return self._all(sql, (*args, limit))

    def security_summary(self, since: float) -> dict:
        """Failed sign-ins since `since` (and from how many addresses) and the number of active admins."""
        r = self._one("SELECT COUNT(*) AS n, COUNT(DISTINCT ip) AS ips FROM audit_log "
                      "WHERE action = 'auth.login_failed' AND at >= ?", (since,))
        admins = self._one("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND disabled = 0")["n"]
        return {"failed_logins": r["n"], "failed_ips": r["ips"], "admins": admins}

    def prune_audit(self, older_than: float) -> int:
        return self._exec("DELETE FROM audit_log WHERE at < ?", (older_than,))

    # -- API keys & gateway usage ---------------------------------------------
    KEY_COLS = "id, user_id, name, prefix, created_at, last_used_at, revoked_at"

    def create_api_key(self, uid: str, name: str, prefix: str, key_hash: str) -> dict:
        kid = new_id()
        self._exec("INSERT INTO api_keys(id, user_id, name, prefix, key_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                   (kid, uid, name, prefix, key_hash, now()))
        return self._one(f"SELECT {self.KEY_COLS} FROM api_keys WHERE id = ?", (kid,))

    def list_api_keys(self, uid: str | None = None) -> list[dict]:
        """One user's keys, or (uid None) every key with its owner, for admins."""
        if uid is not None:
            return self._all(f"SELECT {self.KEY_COLS} FROM api_keys WHERE user_id = ? ORDER BY revoked_at IS NOT NULL, "
                             "created_at DESC", (uid,))
        return self._all(f"SELECT {', '.join('k.' + c for c in self.KEY_COLS.split(', '))}, u.display_name, u.username "
                         "FROM api_keys k JOIN users u ON u.id = k.user_id "
                         "ORDER BY k.revoked_at IS NOT NULL, k.last_used_at DESC")

    def count_active_keys(self, uid: str) -> int:
        return self._one("SELECT COUNT(*) AS n FROM api_keys WHERE user_id = ? AND revoked_at IS NULL", (uid,))["n"]

    def revoke_api_key(self, kid: str, uid: str | None = None) -> bool:
        sql, args = "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (now(), kid)
        if uid is not None:
            sql, args = sql + " AND user_id = ?", (*args, uid)
        return self._exec(sql, args) > 0

    def api_key_user(self, key_hash: str) -> dict | None:
        """The active key and its (enabled) owner, or None."""
        return self._one(
            "SELECT k.id AS key_id, k.last_used_at, u.id, u.username, u.display_name, u.role "
            "FROM api_keys k JOIN users u ON u.id = k.user_id "
            "WHERE k.key_hash = ? AND k.revoked_at IS NULL AND u.disabled = 0", (key_hash,))

    def touch_api_key(self, kid: str) -> None:
        self._exec("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now(), kid))

    def log_api_usage(self, key_id: str, uid: str, model_id: str | None, endpoint: str, status: int,
                      prompt_tokens: int, completion_tokens: int, duration_s: float) -> None:
        self._exec("INSERT INTO api_usage(key_id, user_id, model_id, endpoint, status, prompt_tokens, "
                   "completion_tokens, duration_s, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (key_id, uid, model_id, endpoint, status, prompt_tokens, completion_tokens, round(duration_s, 3),
                    now()))
