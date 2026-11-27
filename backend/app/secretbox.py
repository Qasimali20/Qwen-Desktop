"""Encryption at rest for the credentials the server must be able to use later: git access tokens of connected
repositories and MCP connector headers (e.g. ``Authorization: Bearer …``).

Values are sealed with Fernet (AES-128-CBC + HMAC-SHA256, from ``cryptography``) and stored as
``enc:v1:<token>``. The key lives in a file OUTSIDE the data directory, so neither the database nor the nightly
backups (which copy ``data/``) contain anything usable on their own. Losing the key loses only these
credentials: repositories and connectors then fail with an auth error until someone re-enters the token.

Values written before encryption existed are plain text; ``open`` passes them through and
``Database.encrypt_secrets`` converts them at start-up.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

PREFIX = "enc:v1:"
DEFAULT_KEY_FILE = Path.home() / ".config" / "qwen-desktop" / "secret.key"


class SecretBox:
    def __init__(self, key: bytes):
        self._f = Fernet(key)

    @classmethod
    def from_file(cls, path: Path | None = None) -> SecretBox:
        """Load the key, creating it (mode 600) on first use. Both server processes start at once, so creation is
        an exclusive create: the loser of the race reads the winner's key."""
        path = Path(path or os.environ.get("QWEN_DESKTOP_SECRET_KEY_FILE") or DEFAULT_KEY_FILE)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
            log.warning("created a new encryption key for stored credentials at %s: back it up separately "
                        "(it is deliberately not part of the nightly backup)", path)
        for _ in range(50):                 # the creating process may not have written it yet
            key = path.read_bytes().strip()
            if key:
                return cls(key)
            time.sleep(0.02)
        raise RuntimeError(f"encryption key file {path} is empty")

    def seal(self, value: str | None) -> str | None:
        if not value or value.startswith(PREFIX):
            return value
        return PREFIX + self._f.encrypt(value.encode()).decode()

    def open(self, value: str | None) -> str | None:
        """The plain value. Legacy plain-text values pass through; a value sealed with another key gives None
        (logged), so the caller fails like a missing credential instead of crashing."""
        if not value or not value.startswith(PREFIX):
            return value
        try:
            return self._f.decrypt(value[len(PREFIX):].encode()).decode()
        except InvalidToken:
            log.error("a stored credential cannot be decrypted with the current key (was the key file replaced?)")
            return None


def is_sealed(value: str | None) -> bool:
    return bool(value) and value.startswith(PREFIX)
