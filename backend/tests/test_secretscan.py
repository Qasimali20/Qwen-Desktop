"""Secret detection: real credentials are caught, look-alikes in ordinary code are not.

The same cases are used for frontend/src/lib/secrets.ts (the composer guard); keep both rule sets in step.
Run: uv run --with pytest pytest -q tests
"""
import json
from pathlib import Path

import pytest

from app.secretscan import find, redact

# Samples are stored with "<>" inside each key prefix so the repository holds nothing a secret scanner (e.g.
# GitHub push protection) would take for a live credential; the marker is removed here.
CASES = json.loads((Path(__file__).parent / "secret_cases.json").read_text().replace("<>", ""))


@pytest.mark.parametrize(("kind", "text"), CASES["positive"])
def test_detects(kind: str, text: str) -> None:
    found = find(text)
    assert found and found[0].kind == kind


@pytest.mark.parametrize("text", CASES["negative"])
def test_ignores_lookalikes(text: str) -> None:
    assert find(text) == []


def test_redact_keeps_context_and_hides_values() -> None:
    text = "DATABASE_URL=postgres://admin:S3cr3tPass!@db:5432/a token ghp_" + "aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5z"
    out, findings = redact(text)
    assert [f.kind for f in findings] == ["url_password", "github_token"]
    assert "S3cr3tPass" not in out and "aB3dE5" not in out
    assert out.startswith("DATABASE_URL=postgres://admin:[REDACTED url_password]@db:5432/a")
