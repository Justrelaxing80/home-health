"""Encrypts stored account passwords and session tokens at rest.

The key lives in DATA_DIR/secret.key (created on first run, mode 600) unless
APP_SECRET_KEY is set. Losing the key means re-linking accounts, nothing more.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from . import db

_fernet: Fernet | None = None


def _load() -> Fernet:
    global _fernet
    if _fernet:
        return _fernet
    key = os.environ.get("APP_SECRET_KEY")
    if not key:
        path = db.DATA_DIR / "secret.key"
        if path.exists():
            key = path.read_text().strip()
        else:
            key = Fernet.generate_key().decode()
            path.write_text(key)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(value: str | None) -> str | None:
    if value is None:
        return None
    return _load().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _load().decrypt(value.encode()).decode()
    except InvalidToken:
        return None
