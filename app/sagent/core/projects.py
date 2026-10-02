"""Project registry and membership."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from sagent import db
from sagent.core import audit, rbac, settings
from sagent.core.errors import Conflict, Forbidden, NotFound, ValidationError

LIFECYCLES = ("BOOTSTRAP", "DEVELOPMENT", "TESTING", "REVIEW", "RELEASE", "MAINTENANCE", "ARCHIVED")
AGENTS = ("claude", "codex")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


@dataclass(frozen=True)
class Project:
    id: int
    slug: str
    name: str
    path: str
    description: str
    lifecycle: str
    primary_agent: str
    created_by: int | None
    created_at: str
    updated_at: str
    archived_at: str | None

    @classmethod
    def from_row(cls, row) -> "Project":
        return cls(**{k: row[k] for k in cls.__dataclass_fields__})

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:40].strip("-") or "project"


def _unique_slug(base: str) -> str:
    slug, n = base, 2
    while db.query_one("SELECT 1 FROM projects WHERE slug = ?", (slug,)):
        suffix = f"-{n}"
        slug = base[: 40 - len(suffix)] + suffix
        n += 1
    return slug


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_project_path(actor, raw: str, create: bool = False) -> Path:
    """Normalise and authorise a project directory.

    Admins may register any directory. Members only inside the admin-configured
    allowed roots (checked after resolving symlinks). With create=True a missing
    directory is accepted when its parent exists; the caller creates it.
    """
    raw = (raw or "").strip()
    if not raw or any(c in raw for c in ("\x00", "\n", "\r")) or raw.endswith(";"):
        raise ValidationError("프로젝트 경로를 입력하세요.")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ValidationError("절대 경로를 입력하세요.")
    real = Path(os.path.realpath(p))
    if not real.is_dir():
        if not create or real.exists() or not real.parent.is_dir():
            raise ValidationError("존재하는 디렉터리가 아닙니다." if not create else "상위 디렉터리가 없거나 경로가 파일입니다.")
    if real == Path(real.anchor):
        raise ValidationError("루트 디렉터리는 등록할 수 없습니다.")
    if actor.is_admin:
        return real
    roots = [Path(os.path.realpath(r)) for r in settings.get_lines("projects.allowed_roots")]
    if not roots or not any(_within(real, r) and real != r for r in roots):
        raise Forbidden("허용된 작업 경로 밖입니다. 관리자에게 허용 경로를 요청하세요.")
    return real


def can_create(actor) -> bool:
    return actor.is_admin or (
        settings.get_bool("projects.member_can_create")
        and bool(settings.get_lines("projects.allowed_roots"))
    )


def create(
    actor,
    name: str,
    path: str,
    *,
    slug: str = "",
    description: str = "",
    primary_agent: str = "claude",
    create_dir: bool = False,
) -> Project:
    if not can_create(actor):
        raise Forbidden("프로젝트를 만들 권한이 없습니다.")
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise ValidationError("프로젝트 이름은 1~80자여야 합니다.")
    if primary_agent not in AGENTS:
        raise ValidationError("지원하지 않는 에이전트입니다.")
    real = resolve_project_path(actor, path, create=create_dir)
    for row in db.query("SELECT path FROM projects"):
        other = Path(row["path"])
        if real == other or _within(real, other) or _within(other, real):
            # nested projects would let one team run agents over another's repo
            raise Conflict("이미 등록된 프로젝트와 경로가 같거나 겹칩니다.")
    if slug:
        slug = slug.strip().lower()
        if not SLUG_RE.match(slug):
            raise ValidationError("slug 는 영문 소문자·숫자·- 로 1~40자여야 합니다.")
        if db.query_one("SELECT 1 FROM projects WHERE slug = ?", (slug,)):
            raise Conflict("이미 사용 중인 slug 입니다.")
    else:
        slug = _unique_slug(slugify(name))
    if not real.exists():
        real.mkdir(mode=0o755)
    now = db.utcnow()
    with db.connect() as conn:
        pid = conn.execute(
            "INSERT INTO projects (slug, name, path, description, primary_agent, created_by,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (slug, name, str(real), (description or "").strip()[:500], primary_agent,
             actor.id, now, now),
        ).lastrowid
        if actor.id is not None:
            conn.execute(
                "INSERT INTO project_members (project_id, user_id, role, added_by, created_at)"
                " VALUES (?, ?, 'owner', ?, ?)",
                (pid, actor.id, actor.id, now),
            )
    audit.record("project.create", actor, "project", pid, {"slug": slug, "path": str(real)})
    return _get_by_id(pid)


def _get_by_id(project_id: int) -> Project:
    row = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not row:
        raise NotFound("프로젝트를 찾을 수 없습니다.")
    return Project.from_row(row)


def find_by_path(path: str) -> Project | None:
    """Internal lookup (no authorisation) — callers must authorise afterwards."""
    row = db.query_one("SELECT * FROM projects WHERE path = ?", (os.path.realpath(path),))
    return Project.from_row(row) if row else None


def get(actor, slug: str, perm: str = "project.view") -> tuple[Project, str]:
    """Load a project the actor may see; returns (project, effective_role)."""
    row = db.query_one("SELECT * FROM projects WHERE slug = ?", ((slug or "").lower(),))
    if not row:
        raise NotFound("찾을 수 없습니다.")
    project = Project.from_row(row)
    role = rbac.authorize(actor, project.id, perm)
    return project, role


def list_for(actor, include_archived: bool = False) -> list[tuple[Project, str]]:
    archived = "" if include_archived else " AND p.archived_at IS NULL"
    if actor.is_admin:
        rows = db.query(f"SELECT p.*, 'owner' AS my_role FROM projects p WHERE 1=1{archived} ORDER BY p.name")
    else:
        rows = db.query(
            "SELECT p.*, m.role AS my_role FROM projects p JOIN project_members m"
            f" ON m.project_id = p.id WHERE m.user_id = ?{archived} ORDER BY p.name",
            (actor.id,),
        )
    return [(Project.from_row(r), r["my_role"]) for r in rows]


def update(actor, slug: str, **fields) -> Project:
    project, _ = get(actor, slug, "project.settings")
    changes = {}
    if "name" in fields:
        name = (fields["name"] or "").strip()
        if not name or len(name) > 80:
            raise ValidationError("프로젝트 이름은 1~80자여야 합니다.")
        changes["name"] = name
    if "description" in fields:
        changes["description"] = (fields["description"] or "").strip()[:500]
    if "lifecycle" in fields:
        if fields["lifecycle"] not in LIFECYCLES:
            raise ValidationError("알 수 없는 lifecycle 입니다.")
        changes["lifecycle"] = fields["lifecycle"]
    if "primary_agent" in fields:
        if fields["primary_agent"] not in AGENTS:
            raise ValidationError("지원하지 않는 에이전트입니다.")
        changes["primary_agent"] = fields["primary_agent"]
    if not changes:
        return project
    changes["updated_at"] = db.utcnow()
    cols = ", ".join(f"{k} = ?" for k in changes)
    db.execute(f"UPDATE projects SET {cols} WHERE id = ?", (*changes.values(), project.id))
    audit.record("project.update", actor, "project", project.id, changes)
    return _get_by_id(project.id)


def set_archived(actor, slug: str, archived: bool) -> Project:
    project, _ = get(actor, slug, "project.archive")
    db.execute(
        "UPDATE projects SET archived_at = ?, updated_at = ? WHERE id = ?",
        (db.utcnow() if archived else None, db.utcnow(), project.id),
    )
    audit.record("project.archive" if archived else "project.unarchive", actor, "project", project.id)
    return _get_by_id(project.id)


# --- membership ------------------------------------------------------------

def list_members(actor, slug: str):
    project, _ = get(actor, slug)
    return db.query(
        "SELECT m.user_id, m.role, m.created_at, u.username, u.display_name, u.is_active"
        " FROM project_members m JOIN users u ON u.id = m.user_id WHERE m.project_id = ?"
        " ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'maintainer' THEN 1"
        " WHEN 'developer' THEN 2 ELSE 3 END, u.username",
        (project.id,),
    )


def _owner_count(project_id: int, exclude_user: int | None = None) -> int:
    return db.scalar(
        "SELECT COUNT(*) FROM project_members WHERE project_id = ? AND role = 'owner' AND user_id != ?",
        (project_id, exclude_user or -1),
    ) or 0


def set_member(actor, slug: str, username: str, role: str) -> None:
    from sagent.core import users

    project, _ = get(actor, slug, "member.manage")
    if role not in rbac.PROJECT_ROLES:
        raise ValidationError("알 수 없는 프로젝트 역할입니다.")
    target = users.find(username)
    if not target or not target.is_active:
        raise ValidationError("활성 사용자를 찾을 수 없습니다.")
    current = rbac.member_role(target, project.id)
    if current == "owner" and role != "owner" and _owner_count(project.id, target.id) == 0:
        raise Conflict("마지막 owner 는 강등할 수 없습니다.")
    now = db.utcnow()
    db.execute(
        "INSERT INTO project_members (project_id, user_id, role, added_by, created_at)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT(project_id, user_id) DO UPDATE SET role = excluded.role",
        (project.id, target.id, role, actor.id, now),
    )
    audit.record(
        "member.set", actor, "project", project.id,
        {"user": target.username, "role": role, "previous": current},
    )


def remove_member(actor, slug: str, user_id: int) -> None:
    project, _ = get(actor, slug, "member.manage")
    row = db.query_one(
        "SELECT role FROM project_members WHERE project_id = ? AND user_id = ?",
        (project.id, user_id),
    )
    if not row:
        raise NotFound("멤버가 아닙니다.")
    if row["role"] == "owner" and _owner_count(project.id, user_id) == 0:
        raise Conflict("마지막 owner 는 제거할 수 없습니다.")
    db.execute(
        "DELETE FROM project_members WHERE project_id = ? AND user_id = ?", (project.id, user_id)
    )
    audit.record("member.remove", actor, "project", project.id, {"user_id": user_id})


# --- scans -----------------------------------------------------------------

def save_scan(actor, project, result: dict) -> None:
    import json

    db.execute(
        "INSERT INTO project_scans (project_id, result_json, created_by, created_at) VALUES (?, ?, ?, ?)",
        (project.id, json.dumps(result), actor.id, db.utcnow()),
    )


def last_scan(project) -> dict | None:
    import json

    raw = db.scalar(
        "SELECT result_json FROM project_scans WHERE project_id = ? ORDER BY id DESC LIMIT 1",
        (project.id,),
    )
    return json.loads(raw) if raw else None
