"""Credentials at rest (secretbox + Database) and the attachment serving policy."""
import json
import os

import pytest
from cryptography.fernet import Fernet

from app.db import Database
from app.routers.files import content_policy
from app.secretbox import PREFIX, SecretBox, is_sealed


# ---------------------------------------------------------------- secretbox
def test_seal_open_round_trip_and_idempotence():
    box = SecretBox(Fernet.generate_key())
    sealed = box.seal("ghp_example")
    assert sealed.startswith(PREFIX) and "ghp_example" not in sealed
    assert box.seal(sealed) == sealed                       # never double-encrypted
    assert box.open(sealed) == "ghp_example"


@pytest.mark.parametrize("value", [None, "", "legacy-plain-token"])
def test_empty_and_legacy_values_pass_through(value):
    box = SecretBox(Fernet.generate_key())
    assert box.open(value) == value


def test_wrong_key_gives_none_not_a_crash():
    sealed = SecretBox(Fernet.generate_key()).seal("secret")
    assert SecretBox(Fernet.generate_key()).open(sealed) is None


def test_key_file_is_created_private_and_reused(tmp_path):
    path = tmp_path / "cfg" / "secret.key"
    a = SecretBox.from_file(path)
    b = SecretBox.from_file(path)                             # second process: reads the same key
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert b.open(a.seal("x")) == "x"


# ---------------------------------------------------------------- database
@pytest.fixture
def env(tmp_path):
    box = SecretBox(Fernet.generate_key())
    d = Database(tmp_path / "t.db", box=box)
    uid = d.create_user("u1", "U", "x")["id"]
    pid = d.create_project(uid, "P")["id"]
    return d, uid, pid, tmp_path, box


def _raw(d, sql, *args):
    return d._c.execute(sql, args).fetchone()[0]


def _mcp_fields(headers):
    return {"name": "jira", "label": "Jira", "description": "", "url": "https://mcp.example.com/mcp",
            "transport": "http", "headers": headers, "enabled": 1, "status": "ok", "error": None,
            "checked_at": None, "tool_list": [], "disabled_tools": []}


def test_repo_token_is_encrypted_in_the_database(env):
    d, _, pid, _, _ = env
    r = d.create_repo(pid, "r", "git", "U", url="https://example.com/r.git", auth_token="ghp_secret")
    assert is_sealed(_raw(d, "SELECT auth_token FROM repos WHERE id = ?", r["id"]))
    assert d.get_repo(r["id"], private=True)["auth_token"] == "ghp_secret"
    assert "auth_token" not in d.get_repo(r["id"])
    d.update_repo(r["id"], {"auth_token": "ghp_new", "status": "ready"})
    assert is_sealed(_raw(d, "SELECT auth_token FROM repos WHERE id = ?", r["id"]))
    assert d.repos_due_for_check(1e12)[0]["auth_token"] == "ghp_new"


def test_mcp_headers_are_encrypted_in_the_database(env):
    d, uid, _, _, _ = env
    s = d.create_mcp_server(_mcp_fields({"Authorization": "Bearer tok"}), uid)
    raw = _raw(d, "SELECT headers FROM mcp_servers WHERE id = ?", s["id"])
    assert "tok" not in raw and json.loads(raw).startswith(PREFIX)
    assert d.get_mcp_server(s["id"])["headers"] == {"Authorization": "Bearer tok"}
    assert d.list_mcp_servers()[0]["headers"] == {"Authorization": "Bearer tok"}
    d.update_mcp_server(s["id"], {"headers": {}})
    assert d.get_mcp_server(s["id"])["headers"] == {}
    assert _raw(d, "SELECT headers FROM mcp_servers WHERE id = ?", s["id"]) == "{}"


def test_existing_plain_values_are_converted_once(env):
    _, _, _, tmp, box = env
    plain = Database(tmp / "old.db")                          # written before encryption existed
    uid = plain.create_user("u2", "U", "x")["id"]
    pid = plain.create_project(uid, "P")["id"]
    rid = plain.create_repo(pid, "r", "git", "U", url="https://e.com/r.git", auth_token="ghp_old")["id"]
    sid = plain.create_mcp_server(_mcp_fields({"X-Api-Key": "k"}), uid)["id"]
    plain.create_mcp_server({**_mcp_fields({}), "name": "open", "label": "Open"}, uid)

    d = Database(tmp / "old.db", box=box)
    assert d.plaintext_secret_count() == 2
    assert d.encrypt_secrets() == 2
    assert d.encrypt_secrets() == 0                            # idempotent (the other process finds nothing)
    assert d.plaintext_secret_count() == 0
    assert d.get_repo(rid, private=True)["auth_token"] == "ghp_old"
    assert d.get_mcp_server(sid)["headers"] == {"X-Api-Key": "k"}


# ---------------------------------------------------------------- attachment serving
@pytest.mark.parametrize(("mime", "download", "media", "disposition", "sandboxed"), [
    ("text/html", False, "text/html", "attachment", True),           # never rendered on the app's origin
    ("image/svg+xml", False, "image/svg+xml", "attachment", True),
    ("application/xml", False, "application/xml", "attachment", True),
    ("application/javascript", False, "application/javascript", "attachment", True),
    ("image/png", False, "image/png", "inline", True),
    ("application/pdf", False, "application/pdf", "inline", False),  # Chrome's viewer can't run sandboxed
    ("text/markdown", False, "text/plain; charset=utf-8", "inline", True),
    ("text/csv; charset=utf-8", False, "text/plain; charset=utf-8", "inline", True),
    ("image/png", True, "image/png", "attachment", True),
])
def test_content_policy(mime, download, media, disposition, sandboxed):
    m, disp, headers = content_policy(mime, download)
    assert (m, disp) == (media, disposition)
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert ("sandbox" in headers.get("Content-Security-Policy", "")) is sandboxed
