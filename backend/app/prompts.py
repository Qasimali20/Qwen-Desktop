"""Prompt shortcuts (/commands): reusable templates for everyday tasks.

A template may contain ``{{input}}`` where the user's text goes; otherwise the text is appended
after the template. Messages store a snapshot of the template used, so later edits to a
shortcut never rewrite past conversations.
"""
from __future__ import annotations

import re

from .db import Database

COMMAND_RE = r"^[a-z0-9][a-z0-9-]{0,31}$"
INPUT = re.compile(r"\{\{\s*input\s*\}\}")
SEEDED_KEY = "prompts_seeded_v1"

DEFAULTS: list[tuple[str, str, str, str]] = [
    ("review", "Code review", "Find bugs, security and performance issues",
     "Review the following code as a senior engineer. Report, in order of severity:\n"
     "1. Correctness bugs and unhandled edge cases\n2. Security issues\n3. Performance problems\n"
     "4. Readability / maintainability\n"
     "For each issue give the location, why it matters, and a concrete fix (code). Skip praise and nitpicks "
     "that do not change behaviour. If the code is fine, say so briefly.\n\n{{input}}"),
    ("tests", "Write unit tests", "Thorough tests with edge cases",
     "Write thorough unit tests for the code below. Use the idiomatic test framework for its language "
     "(pytest for Python, Vitest/Jest for TypeScript, JUnit 5 for Java, Go's testing package…). Cover the "
     "happy path, edge cases, error handling and boundary values. Mock external I/O. Output a complete, "
     "runnable test file, then a one-line note on anything that is hard to test and why.\n\n{{input}}"),
    ("explain-error", "Explain an error", "Root cause and fix for an error or stack trace",
     "Explain this error. Give:\n- **What it means** in one or two sentences\n- **Most likely root cause** "
     "(and other plausible causes, ranked)\n- **The fix**, with code or commands\n- **How to confirm** the fix "
     "worked\n\n{{input}}"),
    ("explain", "Explain code", "Walk through what code does",
     "Explain what this code does, for a developer new to it: the purpose first, then a walkthrough of the "
     "important parts, then any non-obvious behaviour, pitfalls or bugs you notice.\n\n{{input}}"),
    ("refactor", "Refactor", "Cleaner code, same behaviour",
     "Refactor this code for clarity and maintainability without changing its behaviour. Keep the public "
     "interface unless it is clearly wrong. Output the full refactored code, then a short bullet list of what "
     "changed and why.\n\n{{input}}"),
    ("sql", "Write SQL", "Turn a request into a SQL query",
     "Write a SQL query for the request below. If the dialect is not stated, use PostgreSQL. If table "
     "definitions are given, use them exactly; otherwise state the schema you assumed. Prefer readable CTEs, "
     "explain the query briefly, and mention indexes that would help.\n\nRequest:\n{{input}}"),
    ("regex", "Write a regex", "Regular expression with explanation and tests",
     "Write a regular expression for the requirement below. Give the pattern, a token-by-token explanation, "
     "examples that should and should not match, and usage in Python and JavaScript. Mention edge cases the "
     "pattern deliberately does not handle.\n\nRequirement:\n{{input}}"),
    ("commit-msg", "Commit message", "Conventional commit message from a diff",
     "Write a git commit message for the change below, following Conventional Commits: a subject line under "
     "72 characters (type(scope): summary, imperative mood), a blank line, then a body explaining what changed "
     "and why, wrapped at 72 columns. Output only the commit message in a code block.\n\n{{input}}"),
    ("docstring", "Add docs", "Docstrings / JSDoc for code",
     "Add clear documentation comments to this code in its language's standard style (Google-style docstrings "
     "for Python, JSDoc/TSDoc for JS/TS, Javadoc for Java…). Document parameters, return values, raised "
     "errors and non-obvious behaviour. Do not change the code itself. Output the full documented code.\n\n"
     "{{input}}"),
    ("summarize", "Summarize", "Concise summary with key points",
     "Summarize the following. Start with a two-sentence overview, then the key points as bullets, then any "
     "decisions, numbers, deadlines or action items.\n\n{{input}}"),
    ("email", "Draft an email", "Professional email from rough notes",
     "Draft a clear, professional email from these notes. Keep it concise, friendly and direct, with a "
     "specific subject line. Output the subject and body only.\n\nNotes:\n{{input}}"),
]


def expand(template: str, text: str) -> str:
    text = text.strip()
    if INPUT.search(template):
        return INPUT.sub(lambda _: text, template).strip()
    return f"{template.strip()}\n\n{text}".strip()


def seed_defaults(db: Database) -> None:
    """Install the default team shortcuts once; admins may edit or delete them afterwards."""
    if db.meta_get(SEEDED_KEY):
        return
    for command, title, description, body in DEFAULTS:
        db.seed_team_prompt(command, title, description, body)
    db.meta_set(SEEDED_KEY, "1")


def snapshot(prompt: dict) -> dict:
    """What a message stores about the shortcut it used."""
    return {k: prompt[k] for k in ("id", "command", "title", "body")}
