"""Symmetric encryption for secrets stored in the DB (Fernet).

The key lives in SAGENT_HOME/keystore.key (0600), generated on first use.
"""
from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from sagent.config import read_or_create_secret

_key_path: Path | None = None
_fernet: Fernet | None = None


def configure(path: Path) -> None:
    global _key_path, _fernet
    _key_path = Path(path)
    _fernet = None


def _get() -> Fernet:
    global _fernet
    if _fernet is None:
        if _key_path is None:
            raise RuntimeError("keystore not configured")
        _fernet = Fernet(read_or_create_secret(_key_path, Fernet.generate_key))
    return _fernet


def encrypt(plain: str) -> str:
    return _get().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _get().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


def mask(plain: str) -> str:
    if not plain:
        return ""
    return "••••" + plain[-4:] if len(plain) > 8 else "••••"
