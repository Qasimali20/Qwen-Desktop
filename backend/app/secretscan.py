"""Detects credentials in text (pasted messages, uploaded documents, indexed code) and masks them.

The rules are mirrored in frontend/src/lib/secrets.ts (the composer warns before a message is sent);
keep the two lists in step. Patterns are written in the subset shared by Python and JavaScript regexes.

Masking keeps a short, non-identifying hint so the model and the user can still tell values apart:
"ghp_…[REDACTED github_token]" — never enough characters to help an attacker.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    kind: str
    label: str
    pattern: re.Pattern
    group: int = 0          # the capture group holding the secret (0: whole match)
    keep: int = 0           # leading characters of the secret kept as a hint (prefixes like "ghp_")


def _r(kind: str, label: str, pattern: str, group: int = 0, keep: int = 0, flags: int = 0) -> Rule:
    return Rule(kind, label, re.compile(pattern, flags), group, keep)


RULES: tuple[Rule, ...] = (
    _r("private_key", "Private key",
       r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|$)"),
    _r("aws_access_key", "AWS access key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", keep=4),
    _r("github_token", "GitHub token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{60,255})\b", keep=4),
    _r("gitlab_token", "GitLab token", r"\bglpat-[A-Za-z0-9_\-]{20,}", keep=6),
    _r("anthropic_key", "Anthropic API key", r"\bsk-ant-[A-Za-z0-9_\-]{20,}", keep=7),
    _r("openai_key", "OpenAI-style API key", r"\bsk-(?!ant-)(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}", keep=3),
    _r("qwen_desktop_key", "Qwen Desktop API key", r"\bqd-[A-Za-z0-9_\-]{40,}", keep=3),
    _r("slack_token", "Slack token", r"\bxox[abposr]-[A-Za-z0-9\-]{10,}", keep=5),
    _r("slack_webhook", "Slack webhook", r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]{20,}"),
    _r("stripe_key", "Stripe secret key", r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}", keep=8),
    _r("google_api_key", "Google API key", r"\bAIza[0-9A-Za-z_\-]{35}\b", keep=4),
    _r("hf_token", "Hugging Face token", r"\bhf_[A-Za-z0-9]{30,}\b", keep=3),
    _r("jwt", "JSON Web Token", r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}", keep=3),
    _r("url_password", "Password in a URL", r"\b[a-zA-Z][a-zA-Z0-9+.\-]{1,20}://[^\s:/@'\"]{1,64}:([^\s@/'\"]{3,128})@", group=1),
    _r("assignment", "Password or secret",
       r"""(?:^|[^A-Za-z0-9_])(?:[A-Za-z0-9_]*(?:password|passwd|pwd|secret|api[_\-]?key|apikey|access[_\-]?token|auth[_\-]?token|private[_\-]?key|client[_\-]?secret|bearer))["']?\s*(?:[:=]|=>|:=)\s*["']?([^\s"'`,;)\]}]{8,})""",
       group=1, flags=re.IGNORECASE),
    _r("bearer_header", "Authorization header", r"\bAuthorization:\s*(?:Bearer|Basic|token)\s+([A-Za-z0-9_\-.=:/+]{16,})", group=1,
       flags=re.IGNORECASE),
)

# Values that look like code or placeholders rather than real secrets.
_PLACEHOLDER = re.compile(
    r"""^(?:\$\{?|\{\{|<|%|process\.env|os\.environ|os\.getenv|env\(|getenv|settings\.|config\.|self\.|this\.|
    \*{3,}|x{4,}|\.{3}|your[_\-]|my[_\-]|example|changeme|placeholder|redacted|dummy|sample|secret$|password$|
    none$|null$|nil$|true$|false$|undefined$|string$|str$|bytes$|optional|required|todo)""",
    re.IGNORECASE | re.VERBOSE)


def _looks_real(value: str) -> bool:
    if _PLACEHOLDER.match(value) or "(" in value or "[" in value or "REDACTED" in value:
        return False
    if re.fullmatch(r"[a-z_]+(?:\.[a-z_]+)*", value):       # an identifier / attribute path: code, not a value
        return False
    return len(set(value)) >= 5


@dataclass(frozen=True)
class Finding:
    kind: str
    label: str
    start: int
    end: int
    line: int
    hint: str


def find(text: str, limit: int = 50) -> list[Finding]:
    """Non-overlapping findings in text order (earlier, more specific rules win overlaps)."""
    if not text:
        return []
    taken: list[tuple[int, int]] = []
    out: list[Finding] = []
    for rule in RULES:
        for m in rule.pattern.finditer(text):
            start, end = m.span(rule.group)
            if start < 0 or end <= start:
                continue
            value = text[start:end]
            if rule.kind in ("assignment", "bearer_header", "url_password") and not _looks_real(value):
                continue
            if any(s < end and start < e for s, e in taken):
                continue
            taken.append((start, end))
            out.append(Finding(rule.kind, rule.label, start, end, text.count("\n", 0, start) + 1,
                               value[:rule.keep]))
            if len(out) >= limit:
                break
    return sorted(out, key=lambda f: f.start)


def mask_value(f: Finding) -> str:
    return f"{f.hint}…[REDACTED {f.kind}]" if f.hint else f"[REDACTED {f.kind}]"


def redact(text: str) -> tuple[str, list[Finding]]:
    findings = find(text, limit=10_000)
    if not findings:
        return text, []
    parts, pos = [], 0
    for f in findings:
        parts += [text[pos:f.start], mask_value(f)]
        pos = f.end
    parts.append(text[pos:])
    return "".join(parts), findings


def summary(findings: list[Finding]) -> list[dict]:
    """Compact, value-free description for metadata: [{kind, label, line}]."""
    return [{"kind": f.kind, "label": f.label, "line": f.line} for f in findings[:20]]
