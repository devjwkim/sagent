"""Runtime settings stored in the DB, edited by admins.

Secret settings are encrypted with the keystore (Fernet) and never returned
in clear text to the UI.
"""
from __future__ import annotations

from sagent import db

DEFAULTS: dict[str, str] = {
    "auth.lockout_threshold": "10",
    "auth.lockout_window_min": "15",
    "projects.member_can_create": "0",
    # Newline-separated absolute directories members may register projects under.
    "projects.allowed_roots": "",
    "runs.log_retention_days": "30",
    "otel.endpoint": "",
    "otel.agent_env": "0",
    "otel.agent_headers": "0",
}

SECRET_KEYS = {"otel.headers"}


def get(key: str) -> str:
    row = db.query_one("SELECT value, is_secret FROM settings WHERE key = ?", (key,))
    if row is None:
        return DEFAULTS.get(key, "")
    if row["is_secret"]:
        from sagent.core import keystore

        return keystore.decrypt(row["value"])
    return row["value"]


def get_int(key: str) -> int:
    try:
        return int(get(key))
    except ValueError:
        return int(DEFAULTS.get(key, "0") or 0)


def get_bool(key: str) -> bool:
    return get(key).strip().lower() in {"1", "true", "yes", "on"}


def get_lines(key: str) -> list[str]:
    return [line.strip() for line in get(key).splitlines() if line.strip()]


def is_set(key: str) -> bool:
    return db.query_one("SELECT 1 FROM settings WHERE key = ?", (key,)) is not None


def put(key: str, value: str, actor=None) -> None:
    from sagent.core import audit

    secret = key in SECRET_KEYS
    stored = value
    if secret and value:
        from sagent.core import keystore

        stored = keystore.encrypt(value)
    db.execute(
        "INSERT INTO settings (key, value, is_secret, updated_by, updated_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, is_secret = excluded.is_secret,"
        " updated_by = excluded.updated_by, updated_at = excluded.updated_at",
        (key, stored, int(secret), getattr(actor, "id", None), db.utcnow()),
    )
    audit.record(
        "settings.update", actor, "setting", key,
        {"key": key} if secret else {"key": key, "new": value[:200]},
    )
