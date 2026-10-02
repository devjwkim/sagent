"""First-run setup: create the first administrator from the browser.

A fresh server with no users generates a one-time token in
SAGENT_HOME/setup_token (0600) and prints a setup link to the console that
started it (or `sagent setup-url`). Only someone with that link can create
the first admin; the token is deleted as soon as an account exists, so the
setup page is gone for good after that.
"""
from __future__ import annotations

import hmac
import secrets
from pathlib import Path

from sagent.config import write_private_file
from sagent.core import audit, users
from sagent.core.errors import Forbidden, NotFound

TOKEN_FILE = "setup_token"


def _path(home: Path) -> Path:
    return Path(home) / TOKEN_FILE


def needed() -> bool:
    return users.count_users() == 0


def ensure_token(home: Path) -> str | None:
    """Return the setup token while setup is pending, else None (and remove it)."""
    p = _path(home)
    if not needed():
        if p.exists():
            p.unlink(missing_ok=True)
        return None
    if p.exists():
        token = p.read_text().strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    write_private_file(p, token.encode())
    return token


def valid(home: Path, token: str | None) -> bool:
    if not token or not needed():
        return False
    p = _path(home)
    if not p.exists():
        return False
    return hmac.compare_digest(p.read_text().strip(), token.strip())


def complete(home: Path, token: str, username: str, password: str, display_name: str = "") -> users.User:
    if not needed():
        raise NotFound("이미 설정이 끝났습니다.")
    if not valid(home, token):
        raise Forbidden("설정 링크가 올바르지 않거나 만료되었습니다.")
    admin = users.create(users.SYSTEM, username, password, role="admin", display_name=display_name,
                         must_change_password=False)
    _path(home).unlink(missing_ok=True)
    audit.record("setup.complete", admin, "user", admin.id, {"username": admin.username})
    return admin


def url(host: str, port: int, token: str) -> str:
    shown = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    return f"http://{shown}:{port}/setup?token={token}"


def lan_hint(host: str) -> str | None:
    """When listening on all interfaces, also suggest this machine's address."""
    if host not in ("0.0.0.0", "::"):
        return None
    try:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET, no packet is sent for UDP connect
            return s.getsockname()[0]
    except OSError:
        return None

