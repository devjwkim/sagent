"""Multi-user permission model.

Two layers: global role (admin | member) x project role
(viewer < developer < maintainer < owner). Admins act as owner on every
project. Anything not visible to a user raises NotFound, so the existence of
other people's projects is never revealed.
"""
from __future__ import annotations

from sagent import db
from sagent.core.errors import Forbidden, NotFound

PROJECT_ROLES = ("viewer", "developer", "maintainer", "owner")
ROLE_RANK = {r: i for i, r in enumerate(PROJECT_ROLES)}

# permission -> minimum project role
PERMS: dict[str, str] = {
    "project.view": "viewer",
    "usage.view": "viewer",
    "terminal.view": "developer",
    "run.start": "developer",
    "run.control.own": "developer",
    "terminal.input.own": "developer",
    "run.control.any": "maintainer",
    "terminal.input.any": "maintainer",
    "harness.edit": "maintainer",
    "loop.edit": "maintainer",
    "prompt.edit": "maintainer",
    "project.settings": "maintainer",
    "member.manage": "owner",
    "project.archive": "owner",
    "project.delete": "owner",
}

ROLE_LABELS = {
    "viewer": "Viewer — 조회만",
    "developer": "Developer — 실행·자기 Run 제어",
    "maintainer": "Maintainer — 정책·Loop 편집, 모든 Run 제어",
    "owner": "Owner — 멤버 관리·보관",
}


def member_role(user, project_id: int) -> str | None:
    if user is None or user.id is None:
        return None
    return db.scalar(
        "SELECT role FROM project_members WHERE project_id = ? AND user_id = ?",
        (project_id, user.id),
    )


def effective_role(user, project_id: int) -> str | None:
    if user is None or not user.is_active:
        return None
    if user.is_admin:
        return "owner"
    return member_role(user, project_id)


def role_allows(role: str | None, perm: str) -> bool:
    if role is None:
        return False
    need = PERMS.get(perm)
    if need is None:
        raise KeyError(f"unknown permission: {perm}")
    return ROLE_RANK[role] >= ROLE_RANK[need]


def can(user, project_id: int, perm: str) -> bool:
    return role_allows(effective_role(user, project_id), perm)


def authorize(user, project_id: int, perm: str) -> str:
    """Raise NotFound if the project is invisible to the user, Forbidden if
    visible but the permission is missing. Returns the effective role."""
    role = effective_role(user, project_id)
    if role is None:
        raise NotFound("찾을 수 없습니다.")
    if not role_allows(role, perm):
        raise Forbidden("이 작업을 할 권한이 없습니다.")
    return role


def permissions_for(role: str | None) -> set[str]:
    return {p for p in PERMS if role_allows(role, p)}


def can_control_run(user, project_id: int, run_owner_id: int | None, kind: str = "run.control") -> bool:
    """kind: 'run.control' or 'terminal.input' — own vs any."""
    role = effective_role(user, project_id)
    if role_allows(role, f"{kind}.any"):
        return True
    return run_owner_id is not None and run_owner_id == user.id and role_allows(role, f"{kind}.own")
