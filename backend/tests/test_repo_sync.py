"""Automatic repository sync: resolving the remote commit and the database-level claims that keep the two
server processes from checking or syncing the same repository twice."""
import time

import pytest

from app.db import Database
from app.repos import pick_remote_sha

LS = """aaaaaaaa\tHEAD
bbbbbbbb\trefs/heads/main
cccccccc\trefs/heads/release
dddddddd\trefs/tags/v1.0
eeeeeeee\trefs/tags/v1.0^{}
ffffffff\trefs/tags/light
"""


@pytest.mark.parametrize(("ref", "expected"), [
    (None, "aaaaaaaa"),            # default branch
    ("HEAD", "aaaaaaaa"),          # a clone of the default branch records "HEAD" when detached
    ("main", "bbbbbbbb"),
    ("release", "cccccccc"),
    ("v1.0", "eeeeeeee"),          # annotated tag: the commit it points to, not the tag object
    ("light", "ffffffff"),         # lightweight tag
    ("missing", None),
])
def test_pick_remote_sha(ref, expected):
    assert pick_remote_sha(LS, ref) == expected


def test_branch_wins_over_tag_of_same_name():
    out = "11111111\trefs/heads/v2\n22222222\trefs/tags/v2\n"
    assert pick_remote_sha(out, "v2") == "11111111"


# ---------------------------------------------------------------- database claims
@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    uid = d.create_user("u1", "U", "x")["id"]
    pid = d.create_project(uid, "P")["id"]
    return d, pid


def _git_repo(d, pid, status="ready", auto=1):
    r = d.create_repo(pid, "r", "git", "U", url="https://example.com/r.git", ref="main")
    d.update_repo(r["id"], {"status": status, "commit_sha": "abc", "auto_sync": auto})
    return r["id"]


def test_due_and_claim_once_per_interval(db):
    d, pid = db
    rid = _git_repo(d, pid)
    before = time.time() - 900
    assert [r["id"] for r in d.repos_due_for_check(before)] == [rid]
    assert d.claim_repo_check(rid, before) is True
    assert d.claim_repo_check(rid, before) is False          # the other process loses the race
    assert d.repos_due_for_check(before) == []                # not due again until the interval passes


@pytest.mark.parametrize(("status", "auto"), [("queued", 1), ("indexing", 1), ("ready", 0)])
def test_busy_or_switched_off_repos_are_not_checked(db, status, auto):
    d, pid = db
    _git_repo(d, pid, status=status, auto=auto)
    assert d.repos_due_for_check(time.time()) == []


def test_zip_repos_are_never_checked(db):
    d, pid = db
    r = d.create_repo(pid, "z", "zip", "U")
    d.update_repo(r["id"], {"status": "ready"})
    assert d.repos_due_for_check(time.time()) == []


def test_queue_sync_only_from_idle(db):
    d, pid = db
    rid = _git_repo(d, pid)
    assert d.queue_repo_sync(rid) is True
    assert d.get_repo(rid)["status"] == "queued"
    assert d.queue_repo_sync(rid) is False                    # a manual sync already queued it


def test_public_columns_expose_auto_sync_but_not_token(db):
    d, pid = db
    rid = _git_repo(d, pid)
    r = d.get_repo(rid)
    assert r["auto_sync"] == 1 and "checked_at" in r and "auth_token" not in r
