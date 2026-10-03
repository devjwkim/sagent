"""User accounts and password authentication."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from werkzeug.security import check_password_hash, generate_password_hash

from sagent import db
from sagent.core import audit, settings
from sagent.core.errors import Conflict, Forbidden, NotFound, ValidationError

ROLES = ("admin", "member")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,31}$")
MIN_PASSWORD_LEN = 9

# Hash used to keep timing similar when the username does not exist.
_DUMMY_HASH = generate_password_hash("sagent-dummy-password-for-timing")


@dataclass(frozen=True)
class User:
    id: int | None
    username: str
    display_name: str = ""
    role: str = "member"
    is_active: bool = True
    must_change_password: bool = False
    session_epoch: int = 0
    created_at: str = ""
    last_login_at: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def label(self) -> str:
        return self.display_name or self.username

    @classmethod
    def from_row(cls, row) -> "User":
        return cls(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            role=row["role"],
            is_active=bool(row["is_active"]),
            must_change_password=bool(row["must_change_password"]),
            session_epoch=row["session_epoch"],
            created_at=row["created_at"],
            last_login_at=row["last_login_at"],
        )


# Actor used by the local CLI (trusted: it already has filesystem access to the DB).
SYSTEM = User(id=None, username="system", display_name="local operator", role="admin")


def validate_username(username: str) -> str:
    username = (username or "").strip().lower()
    if not USERNAME_RE.match(username):
        raise ValidationError(
            "아이디는 영문 소문자/숫자로 시작하고 영문 소문자·숫자·_.- 로 2~32자여야 합니다."
        )
    return username


def validate_password(password: str, username: str = "") -> None:
    if len(password or "") < MIN_PASSWORD_LEN:
        raise ValidationError(f"비밀번호는 {MIN_PASSWORD_LEN}자 이상이어야 합니다.")
    if username and username.lower() in password.lower():
        raise ValidationError("비밀번호에 아이디를 포함할 수 없습니다.")


def _require_admin(actor: User) -> None:
    if not actor.is_admin:
        raise Forbidden("관리자만 할 수 있습니다.")


def count_users() -> int:
    return db.scalar("SELECT COUNT(*) FROM users") or 0


def get(user_id: int) -> User:
    row = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not row:
        raise NotFound("사용자를 찾을 수 없습니다.")
    return User.from_row(row)


def find(username: str) -> User | None:
    row = db.query_one(
        "SELECT * FROM users WHERE username = ?", ((username or "").strip().lower(),)
    )
    return User.from_row(row) if row else None


def list_all() -> list[User]:
    return [User.from_row(r) for r in db.query("SELECT * FROM users ORDER BY username")]


def create(
    actor: User,
    username: str,
    password: str,
    role: str = "member",
    display_name: str = "",
    must_change_password: bool = True,
) -> User:
    _require_admin(actor)
    username = validate_username(username)
    if role not in ROLES:
        raise ValidationError("알 수 없는 역할입니다.")
    validate_password(password, username)
    if find(username):
        raise Conflict("이미 존재하는 아이디입니다.")
    now = db.utcnow()
    uid = db.execute(
        "INSERT INTO users (username, display_name, password_hash, role, must_change_password,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            username,
            (display_name or "").strip()[:64],
            generate_password_hash(password),
            role,
            int(must_change_password),
            now,
            now,
        ),
    )
    audit.record("user.create", actor, "user", uid, {"username": username, "role": role})
    return get(uid)


def _active_admin_count(exclude_id: int | None = None) -> int:
    return db.scalar(
        "SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1 AND id != ?",
        (exclude_id or -1,),
    ) or 0


def update(
    actor: User,
    user_id: int,
    *,
    role: str | None = None,
    is_active: bool | None = None,
    display_name: str | None = None,
) -> User:
    target = get(user_id)
    if actor.id != user_id:
        _require_admin(actor)
    elif role is not None or is_active is not None:
        _require_admin(actor)
    changes: dict = {}
    if role is not None and role != target.role:
        if role not in ROLES:
            raise ValidationError("알 수 없는 역할입니다.")
        changes["role"] = role
    if is_active is not None and is_active != target.is_active:
        changes["is_active"] = int(is_active)
    if display_name is not None:
        changes["display_name"] = display_name.strip()[:64]
    demoting = changes.get("role", target.role) != "admin" or not changes.get(
        "is_active", int(target.is_active)
    )
    if target.is_admin and target.is_active and demoting and _active_admin_count(user_id) == 0:
        raise Conflict("마지막 활성 관리자는 강등하거나 비활성화할 수 없습니다.")
    if not changes:
        return target
    if "role" in changes or "is_active" in changes:
        changes["session_epoch"] = target.session_epoch + 1  # force re-login
    changes["updated_at"] = db.utcnow()
    cols = ", ".join(f"{k} = ?" for k in changes)
    db.execute(f"UPDATE users SET {cols} WHERE id = ?", (*changes.values(), user_id))
    audit.record(
        "user.update", actor, "user", user_id,
        {k: v for k, v in changes.items() if k not in {"updated_at", "session_epoch"}},
    )
    return get(user_id)


def delete(actor: User, user_id: int) -> None:
    """Remove an account. History (runs, usage, audit) keeps the numeric id
    and is shown as a deleted user."""
    _require_admin(actor)
    target = get(user_id)
    if actor.id == user_id:
        raise Conflict("자기 자신은 삭제할 수 없습니다.")
    if target.is_admin and target.is_active and _active_admin_count(user_id) == 0:
        raise Conflict("마지막 활성 관리자는 삭제할 수 없습니다.")
    sole = db.query(
        "SELECT p.name FROM project_members m JOIN projects p ON p.id = m.project_id"
        " WHERE m.user_id = ? AND m.role = 'owner' AND NOT EXISTS (SELECT 1 FROM project_members o"
        " WHERE o.project_id = m.project_id AND o.role = 'owner' AND o.user_id != m.user_id)", (user_id,))
    if sole:
        raise Conflict("유일한 owner 인 프로젝트가 있어 삭제할 수 없습니다: " + ", ".join(r["name"] for r in sole[:5])
                       + ". 먼저 다른 owner 를 지정하세요.")
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    audit.record("user.delete", actor, "user", user_id, {"username": target.username})


def _set_password(user_id: int, password: str, must_change: bool) -> None:
    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = ?,"
        " session_epoch = session_epoch + 1, updated_at = ? WHERE id = ?",
        (generate_password_hash(password), int(must_change), db.utcnow(), user_id),
    )


def change_password(actor: User, current: str, new: str, ip: str = "") -> User:
    if is_locked(actor.username, ip):
        raise ValidationError("잠시 후 다시 시도하세요.")
    row = db.query_one("SELECT password_hash FROM users WHERE id = ?", (actor.id,))
    if not row or not check_password_hash(row["password_hash"], current or ""):
        db.execute("INSERT INTO login_attempts (username, ip, success, created_at) VALUES (?, ?, 0, ?)",
                   (actor.username, ip, db.utcnow()))
        raise ValidationError("현재 비밀번호가 올바르지 않습니다.")
    if current == new:
        raise ValidationError("새 비밀번호가 현재 비밀번호와 같습니다.")
    validate_password(new, actor.username)
    _set_password(actor.id, new, must_change=False)
    audit.record("user.password_change", actor, "user", actor.id)
    return get(actor.id)


def reset_password(actor: User, user_id: int, new: str) -> User:
    _require_admin(actor)
    target = get(user_id)
    validate_password(new, target.username)
    _set_password(user_id, new, must_change=True)
    audit.record("user.password_reset", actor, "user", user_id)
    return get(user_id)


# --- login & lockout -------------------------------------------------------

def _since(minutes: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).replace(microsecond=0).isoformat()


def is_locked(username: str, ip: str) -> bool:
    threshold = settings.get_int("auth.lockout_threshold")
    window = settings.get_int("auth.lockout_window_min")
    if threshold <= 0:
        return False
    since = _since(window)
    # Count by IP and by username separately: either one reaching the threshold locks.
    by_ip = db.scalar(
        "SELECT COUNT(*) FROM login_attempts WHERE success = 0 AND created_at >= ? AND ip = ?",
        (since, ip),
    ) or 0
    by_user = db.scalar(
        "SELECT COUNT(*) FROM login_attempts WHERE success = 0 AND created_at >= ? AND username = ?",
        (since, (username or "").lower()),
    ) or 0
    # A username lock lets anyone lock a known account out, so it uses a higher
    # bar; per-IP is the main defence. Admins can clear locks (UI / CLI).
    return by_ip >= threshold or by_user >= threshold * 3


def authenticate(username: str, password: str, ip: str) -> User | None:
    """Return the user on success, None otherwise. Never reveals which part failed."""
    username = (username or "").strip().lower()[:64]
    if is_locked(username, ip):
        audit.record("auth.locked", None, "user", username, ip=ip)
        return None
    row = db.query_one("SELECT * FROM users WHERE username = ?", (username,))
    ok = check_password_hash(row["password_hash"] if row else _DUMMY_HASH, password or "")
    ok = ok and row is not None and bool(row["is_active"])
    db.execute(
        "INSERT INTO login_attempts (username, ip, success, created_at) VALUES (?, ?, ?, ?)",
        (username, ip, int(ok), db.utcnow()),
    )
    if not ok:
        audit.record("auth.login_failed", None, "user", username, ip=ip)
        return None
    db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (db.utcnow(), row["id"]))
    user = get(row["id"])
    audit.record("auth.login", user, "user", user.id, ip=ip)
    return user


def clear_lockouts(actor: User, ip: str | None = None) -> int:
    _require_admin(actor)
    with db.connect() as conn:
        if ip:
            cur = conn.execute("DELETE FROM login_attempts WHERE success = 0 AND ip = ?", (ip,))
        else:
            cur = conn.execute("DELETE FROM login_attempts WHERE success = 0")
        n = cur.rowcount
    audit.record("auth.clear_lockouts", actor, "ip", ip or "*", {"cleared": n})
    return n
