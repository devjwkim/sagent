"""Personal API tokens for the JSON API (CI, scripts, other tools).

A token acts as its user with the same project permissions. Only a SHA-256
hash is stored; the token itself is shown once. Scopes: `read` (GET only) or
`write`.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sagent import db
from sagent.core import audit, users
from sagent.core.errors import Forbidden, NotFound, ValidationError

db.register_schema("""
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'read',
    created_at TEXT NOT NULL,
    expires_at TEXT,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id);
""")

PREFIX = "sat_"
SCOPES = ("read", "write")
MAX_PER_USER = 20


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(actor, name: str, scope: str = "read", days: int | None = 90) -> tuple[str, int]:
    if actor.id is None:
        raise ValidationError("로컬 운영자 계정에는 토큰을 만들 수 없습니다.")
    name = (name or "").strip()[:60]
    if not name:
        raise ValidationError("토큰 이름을 입력하세요.")
    if scope not in SCOPES:
        raise ValidationError("알 수 없는 scope 입니다.")
    if (db.scalar("SELECT COUNT(*) FROM api_tokens WHERE user_id = ? AND revoked_at IS NULL", (actor.id,)) or 0) \
            >= MAX_PER_USER:
        raise ValidationError(f"활성 토큰은 {MAX_PER_USER}개까지 만들 수 있습니다.")
    token = PREFIX + secrets.token_urlsafe(32)
    expires = None
    if days:
        expires = (datetime.now(timezone.utc) + timedelta(days=int(days))).replace(microsecond=0).isoformat()
    tid = db.execute(
        "INSERT INTO api_tokens (user_id, name, token_hash, prefix, scope, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (actor.id, name, _hash(token), token[:12], scope, db.utcnow(), expires),
    )
    audit.record("token.create", actor, "api_token", tid, {"name": name, "scope": scope})
    return token, tid


def list_for(actor):
    return db.query("SELECT id, name, prefix, scope, created_at, expires_at, last_used_at, revoked_at"
                    " FROM api_tokens WHERE user_id = ? ORDER BY id DESC", (actor.id,))


def revoke(actor, token_id: int) -> None:
    row = db.query_one("SELECT user_id FROM api_tokens WHERE id = ?", (token_id,))
    if not row:
        raise NotFound("찾을 수 없습니다.")
    if row["user_id"] != actor.id and not actor.is_admin:
        raise Forbidden("본인 토큰만 폐기할 수 있습니다.")
    db.execute("UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (db.utcnow(), token_id))
    audit.record("token.revoke", actor, "api_token", token_id)


def authenticate(token: str) -> tuple[users.User, str] | None:
    """Return (user, scope) for a valid, unexpired, unrevoked token."""
    if not token or not token.startswith(PREFIX) or len(token) > 200:
        return None
    h = _hash(token)
    row = db.query_one("SELECT * FROM api_tokens WHERE token_hash = ?", (h,))
    if not row or not hmac.compare_digest(row["token_hash"], h) or row["revoked_at"]:
        return None
    if row["expires_at"] and row["expires_at"] < db.utcnow():
        return None
    try:
        user = users.get(row["user_id"])
    except NotFound:
        return None
    if not user.is_active:
        return None
    db.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (db.utcnow(), row["id"]))
    return user, row["scope"]
