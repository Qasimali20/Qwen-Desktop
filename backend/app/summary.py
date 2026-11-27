"""Rolling summaries for conversations that outgrow the model's context window.

Without this, a long chat silently loses its oldest turns. Instead, once the turns after the current summary
no longer fit, everything older than a recent window is condensed (together with the previous summary) into a
new summary stored on the conversation. The model then sees: system prompt + <conversation_summary> + the
recent turns verbatim.

Hysteresis keeps this cheap: when a summary is (re)built, the verbatim window is cut to KEEP_SHARE of the
budget, so the next several turns fit without summarising again, instead of re-summarising on every message.

The stored summary names the id of the last message it covers. Editing or regenerating an earlier message
deletes that message (new ids are issued), so a stale summary is never used: `valid_summary` rejects it, and
the database clears it on truncation as well.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any

from .config import ChatModel
from .llm import LLMClient, LLMError

log = logging.getLogger(__name__)

KEEP_SHARE = 0.6                # verbatim window after a (re)summary, as a share of the budget
SUMMARY_MAX_TOKENS = 2000       # length cap of a summary (also reserved in the budget)
MIN_SUMMARY_TOKENS = 400        # small-context models get budget // 4, but never less than this
MIN_WINDOW_TOKENS = 1000        # below this budget, summarising can't help: fall back to dropping turns
PIECE_SHARE = 0.6               # share of the summarising model's context one transcript piece may use
MAX_PIECES = 12                 # caps the work for one enormous backlog (the rest stays summarised next time)
TOOL_ARGS_CHARS = 300
TOOL_RESULT_CHARS = 800
DOC_CHARS = 2500                # of each attached document's text, in the transcript to be summarised

SYSTEM = """You maintain a running summary of a conversation between a user and an AI assistant, so the \
assistant can continue it after older messages are removed from its context.

Write the updated summary as concise Markdown, in the language of the conversation, using exactly these \
section headings, in this order, and leaving out any section that would be empty:

## Goal and context
## Decisions
## Key facts
## Work done
## Open items
## User preferences

What belongs in each: Goal and context: what the user is trying to do, their role, environment and \
constraints. Decisions: what was agreed, chosen or ruled out, and why. Key facts: exact names, numbers, file \
paths, identifiers, URLs, commands, versions and error messages. Work done: what the assistant produced or \
changed (files, code, documents), by name. Open items: unanswered questions and next steps. User preferences: \
style, format or tone the user asked for.

Rules: keep every fact that may matter later and drop small talk and filler; prefer exact values to \
paraphrase; merge the previous summary with the new messages (newer information wins when they conflict); \
never invent anything, and only record numbers that appear in the messages (do not count, estimate \
or compute statistics yourself); stay under {words} words. Reply with the summary only."""

USER = """<previous_summary>
{previous}
</previous_summary>

<new_messages>
{transcript}
</new_messages>

Write the updated summary."""

NOTE = """<conversation_summary covers="{count} earlier messages">
Older parts of this conversation were condensed into the summary below. The messages after it are verbatim. \
Rely on the summary for earlier context; if a detail you need is not in it, say so rather than guessing.

{text}
</conversation_summary>"""


# ---------------------------------------------------------------- planning (pure)
def window_start(roles: Sequence[str], costs: Sequence[int], budget: int) -> int:
    """Index of the first message of the newest run of whole messages that fits in `budget` tokens.

    The latest message is always kept, even alone over budget. The window starts on a user message, so the
    model never sees an assistant turn without the question it answered."""
    n = len(roles)
    if n == 0:
        return 0
    used, start = 0, n
    for i in range(n - 1, -1, -1):
        if start < n and used + costs[i] > budget:
            break
        used += costs[i]
        start = i
    while start < n - 1 and roles[start] != "user":
        start += 1
    return start


def valid_summary(conv: dict, history: Sequence[dict]) -> tuple[dict | None, int]:
    """(summary, index of the first message after it) when the stored summary still matches the history."""
    s = conv.get("context_summary")
    if not s or not s.get("text"):
        return None, 0
    for i, m in enumerate(history):
        if m["id"] == s.get("upto_id"):
            return s, i + 1
    return None, 0


def note(summary: dict) -> str:
    return NOTE.format(count=summary.get("count", "?"), text=summary["text"].strip())


def summary_tokens(budget: int) -> int:
    return max(MIN_SUMMARY_TOKENS, min(SUMMARY_MAX_TOKENS, budget // 4))


# ---------------------------------------------------------------- transcript
def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + f" … [{len(text) - n} more chars]"


def transcript_line(msg: dict, atts: dict[str, dict], text_of_user) -> str:
    """One message as plain text for the summariser: user text with attachment excerpts; assistant text with
    tool calls and (clipped) results. Reasoning is left out."""
    if msg["role"] == "user":
        parts = [text_of_user(msg)]
        for aid in msg["attachment_ids"]:
            a = atts.get(aid)
            if not a:
                continue
            label = "image" if a["kind"] == "image" else "file"
            parts.append(f"[{label} {a['filename']}]" + (f"\n{_clip(a.get('text') or '', DOC_CHARS)}" if a.get("text") else ""))
        if skill := (msg.get("meta") or {}).get("skill"):
            parts.append(f"[used skill {skill.get('name')}]")
        return "USER:\n" + "\n".join(p for p in parts if p)
    parts = []
    for b in msg.get("blocks") or []:
        if b["type"] == "text" and b.get("text", "").strip():
            parts.append(b["text"].strip())
        elif b["type"] == "tool":
            parts.append(f"[tool {b['name']}({_clip(b.get('arguments') or '', TOOL_ARGS_CHARS)}) → "
                         f"{_clip(b.get('result') or '', TOOL_RESULT_CHARS)}]")
    return "ASSISTANT:\n" + ("\n".join(parts) or (msg.get("content") or "(no response)"))


def pieces(lines: Sequence[str], max_chars: int) -> list[str]:
    """Group transcript lines into pieces of at most `max_chars` (a single oversized line is clipped)."""
    out: list[str] = []
    cur: list[str] = []
    size = 0
    for line in lines:
        line = _clip(line, max_chars)
        if cur and size + len(line) > max_chars:
            out.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 2
    if cur:
        out.append("\n\n".join(cur))
    return out


# ---------------------------------------------------------------- summarising
async def summarize(llm: LLMClient, model: ChatModel, previous: str | None, lines: Sequence[str],
                    max_tokens: int) -> str | None:
    """Fold `lines` into `previous`, piece by piece. Returns the new summary, or None if the model failed."""
    max_chars = int((model.context - max_tokens - 1500) * PIECE_SHARE) * 3   # ~3 chars per token
    if max_chars < 2000:
        return None
    text = previous or "(none yet)"
    system = SYSTEM.format(words=int(max_tokens * 0.6))
    for piece in pieces(lines, max_chars)[:MAX_PIECES]:
        try:
            out = await llm.complete(model, [{"role": "system", "content": system},
                                             {"role": "user", "content": USER.format(previous=text, transcript=piece)}],
                                     max_tokens=max_tokens, temperature=0.2)
        except (LLMError, KeyError, ValueError):   # unreachable, refused, or a malformed reply
            log.warning("summarising with %s failed", model.id, exc_info=True)
            return None
        out = (out or "").strip()
        if not out:
            return None
        text = out
    return text


def record(text: str, upto: dict, count: int, model_id: str) -> dict:
    return {"text": text, "upto_id": upto["id"], "upto_seq": upto["seq"], "count": count,
            "model_id": model_id, "at": time.time()}


def public(summary: dict | None) -> dict[str, Any] | None:
    return None if not summary else {k: summary[k] for k in ("text", "upto_id", "upto_seq", "count", "at") if k in summary}


