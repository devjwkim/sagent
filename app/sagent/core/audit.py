"""Append-only audit trail. Never stores passwords or tokens."""
from __future__ import annotations

import json
from typing import Any

from sagent import db

_REDACT_KEYS = {"password", "current", "new", "token", "secret", "csrf_token", "value"}


def _redact(detail: Any) -> Any:
    if isinstance(detail, dict):
        return {
            k: ("[redacted]" if any(s in k.lower() for s in _REDACT_KEYS) else _redact(v))
            for k, v in detail.items()
        }
    if isinstance(detail, (list, tuple)):
        return [_redact(v) for v in detail]
    return detail


def record(
    action: str,
    actor=None,
    target_type: str = "",
    target_id: Any = "",
    detail: Any = None,
    *,
    method: str = "",
    path: str = "",
    ip: str = "",
    status: int | None = None,
) -> None:
    db.execute(
        "INSERT INTO audit_log (user_id, username, action, target_type, target_id, method,"
        " path, ip, status, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            getattr(actor, "id", None),
            getattr(actor, "username", "") or "",
            action,
            target_type,
            str(target_id if target_id is not None else ""),
            method,
            path[:300],
            ip,
            status,
            json.dumps(_redact(detail), ensure_ascii=False)[:2000] if detail else "",
            db.utcnow(),
        ),
    )


def list_entries(limit: int = 100, offset: int = 0, action: str = "", username: str = ""):
    sql = "SELECT * FROM audit_log WHERE 1=1"
    params: list = []
    if action:
        sql += " AND action LIKE ?"
        params.append(f"{action}%")
    if username:
        sql += " AND username = ?"
        params.append(username)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params += [min(max(limit, 1), 500), max(offset, 0)]
    return db.query(sql, tuple(params))
