"""Rolling conversation summaries: window planning, staleness, transcript building and DB invalidation."""
import pytest

from app import summary
from app.db import Database


# ---------------------------------------------------------------- window_start
@pytest.mark.parametrize(("roles", "costs", "budget", "expected"), [
    ([], [], 100, 0),                                                   # empty history
    (["user", "assistant"], [10, 10], 100, 0),                          # everything fits
    (["user", "assistant", "user", "assistant"], [40, 40, 40, 40], 100, 2),   # keeps the newest whole turns
    (["user", "assistant", "user"], [10, 10, 500], 100, 2),             # latest message kept even over budget
    (["user", "assistant", "user", "assistant"], [10, 50, 10, 50], 70, 2),
    # a cut landing on an assistant message moves forward to the next user message
    (["user", "assistant", "user", "assistant"], [10, 10, 10, 10], 30, 2),
    (["user", "assistant", "assistant", "user"], [10, 10, 10, 10], 30, 3),
])
def test_window_start(roles, costs, budget, expected):
    assert summary.window_start(roles, costs, budget) == expected


def test_window_shrinks_monotonically_with_budget():
    roles = ["user", "assistant"] * 20 + ["user"]      # a reply is always generated for a new user message
    costs = [30, 70] * 20 + [30]
    cuts = [summary.window_start(roles, costs, b) for b in range(50, 4000, 50)]
    assert cuts == sorted(cuts, reverse=True)
    assert all(roles[c] == "user" for c in cuts)


# ---------------------------------------------------------------- valid_summary
def _hist(n):
    return [{"id": f"m{i}", "seq": i, "role": "user" if i % 2 == 0 else "assistant"} for i in range(n)]


def test_valid_summary_points_after_covered_message():
    h = _hist(6)
    s, start = summary.valid_summary({"context_summary": {"text": "x", "upto_id": "m3"}}, h)
    assert s and start == 4


@pytest.mark.parametrize("conv", [
    {}, {"context_summary": None}, {"context_summary": {"text": "", "upto_id": "m1"}},
    {"context_summary": {"text": "x", "upto_id": "gone"}},              # message was edited away
])
def test_invalid_summaries_are_ignored(conv):
    assert summary.valid_summary(conv, _hist(4)) == (None, 0)


# ---------------------------------------------------------------- transcript + pieces
def test_transcript_line_includes_tools_and_attachments_but_not_reasoning():
    atts = {"a1": {"kind": "document", "filename": "spec.pdf", "text": "Budget is 40k"}}
    user = {"role": "user", "attachment_ids": ["a1"], "content": "Read this", "meta": {}}
    line = summary.transcript_line(user, atts, lambda m: m["content"])
    assert line.startswith("USER:") and "spec.pdf" in line and "Budget is 40k" in line

    asst = {"role": "assistant", "content": "", "blocks": [
        {"type": "reasoning", "text": "secret thoughts"},
        {"type": "text", "text": "Done."},
        {"type": "tool", "name": "run_python", "arguments": '{"code": "1+1"}', "result": "2"},
    ]}
    line = summary.transcript_line(asst, {}, lambda m: "")
    assert "Done." in line and "run_python" in line and "→ 2" in line and "secret thoughts" not in line


def test_pieces_respect_size_and_keep_order():
    lines = [f"line {i} " + "x" * 90 for i in range(30)]
    ps = summary.pieces(lines, 500)
    assert all(len(p) <= 500 + 100 for p in ps)
    assert "\n\n".join(ps).split("\n\n") == [summary._clip(x, 500) for x in lines]


def test_summary_tokens_scale_with_budget():
    assert summary.summary_tokens(200_000) == summary.SUMMARY_MAX_TOKENS
    assert summary.summary_tokens(4000) == 1000                 # budget // 4 in between
    assert summary.summary_tokens(1200) == summary.MIN_SUMMARY_TOKENS


# ---------------------------------------------------------------- database invalidation
@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    uid = d.create_user("u1", "U", "x")["id"]
    conv = d.create_conversation(uid, None, None)
    for i in range(6):
        d.add_message(conv["id"], "user" if i % 2 == 0 else "assistant", f"m{i}")
    return d, conv["id"]


def _store(d, cid, upto_seq):
    m = next(m for m in d.messages(cid) if m["seq"] == upto_seq)
    d.set_context_summary(cid, summary.record("S", m, upto_seq + 1, "model"))


def test_summary_round_trips(db):
    d, cid = db
    _store(d, cid, 3)
    s = d.get_conversation(cid)["context_summary"]
    assert s["text"] == "S" and s["upto_seq"] == 3


def test_truncating_inside_summary_clears_it(db):
    d, cid = db
    _store(d, cid, 3)
    d.truncate_from(cid, 2)                          # regenerate from a summarised turn
    assert d.get_conversation(cid)["context_summary"] is None


def test_truncating_after_summary_keeps_it(db):
    d, cid = db
    _store(d, cid, 1)
    d.truncate_from(cid, 4)
    assert d.get_conversation(cid)["context_summary"]["upto_seq"] == 1


def test_edit_inside_summary_clears_it(db):
    d, cid = db
    _store(d, cid, 3)
    d.replace_from(cid, 0, "edited first message", [])
    assert d.get_conversation(cid)["context_summary"] is None
