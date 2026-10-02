"""Runtime configuration.

Precedence: explicit arguments > environment variables > defaults.
No secret ever lives in the repository: the Flask secret key and the
keystore key are generated on first run inside SAGENT_HOME with 0600 perms.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17832
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _private_mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def write_private_file(path: Path, data: bytes) -> None:
    """Create a file readable only by the owner (atomic replace)."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def read_or_create_secret(path: Path, factory) -> bytes:
    if path.exists():
        return path.read_bytes().strip()
    value = factory()
    write_private_file(path, value)
    return value


def default_home() -> Path:
    env = os.environ.get("SAGENT_HOME")
    return Path(env).expanduser() if env else Path.home() / ".sagent"


@dataclass
class Config:
    home: Path = field(default_factory=default_home)
    host: str = field(default_factory=lambda: os.environ.get("SAGENT_HOST", DEFAULT_HOST))
    port: int = field(default_factory=lambda: int(os.environ.get("SAGENT_PORT", DEFAULT_PORT)))
    trust_proxy: bool = field(default_factory=lambda: _truthy(os.environ.get("SAGENT_TRUST_PROXY")))
    secure_cookies: bool = field(default_factory=lambda: _truthy(os.environ.get("SAGENT_SECURE_COOKIES")))

    def __post_init__(self) -> None:
        self.home = _private_mkdir(Path(self.home).expanduser().resolve())
        _private_mkdir(self.runs_dir)

    @property
    def db_path(self) -> Path:
        return self.home / "app.db"

    @property
    def runs_dir(self) -> Path:
        return self.home / "runs"

    @property
    def keystore_path(self) -> Path:
        return self.home / "keystore.key"

    def secret_key(self) -> str:
        env = os.environ.get("SAGENT_SECRET_KEY")
        if env:
            return env
        raw = read_or_create_secret(
            self.home / "secret_key", lambda: secrets.token_hex(32).encode()
        )
        return raw.decode()

    @property
    def is_loopback(self) -> bool:
        return self.host in LOOPBACK_HOSTS
