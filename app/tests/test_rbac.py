import pytest

from sagent.core import projects, rbac, users
from sagent.core.errors import Conflict, Forbidden, NotFound

EXPECTED = {
    "viewer": {"project.view", "usage.view"},
    "developer": {"project.view", "usage.view", "terminal.view", "run.start",
                  "run.control.own", "terminal.input.own"},
    "maintainer": {"project.view", "usage.view", "terminal.view", "run.start",
                   "run.control.own", "terminal.input.own", "run.control.any",
                   "terminal.input.any", "harness.edit", "loop.edit", "prompt.edit",
                   "project.settings"},
    "owner": set(rbac.PERMS),
}


@pytest.mark.parametrize("role", rbac.PROJECT_ROLES)
def test_permission_matrix(role):
    assert rbac.permissions_for(role) == EXPECTED[role]


def test_no_role_has_no_permissions():
    assert rbac.permissions_for(None) == set()


def test_unknown_permission_raises():
    with pytest.raises(KeyError):
        rbac.role_allows("owner", "does.not.exist")


@pytest.fixture
def team(app, make_user, workspace):
    admin = make_user("admin1", "admin")
    alice = make_user("alice")
    bob = make_user("bob")
    carol = make_user("carol")
    p = projects.create(admin, "Proj One", str(workspace / "proj1"))
    projects.set_member(admin, p.slug, "alice", "owner")
    projects.set_member(admin, p.slug, "bob", "developer")
    return dict(admin=admin, alice=alice, bob=bob, carol=carol, project=p)


def test_non_member_gets_not_found(team):
    with pytest.raises(NotFound):
        projects.get(team["carol"], team["project"].slug)


def test_member_without_permission_gets_forbidden(team):
    with pytest.raises(Forbidden):
        projects.get(team["bob"], team["project"].slug, "member.manage")


def test_admin_is_owner_everywhere(team):
    _, role = projects.get(team["admin"], team["project"].slug)
    assert role == "owner"


def test_inactive_user_loses_access(team):
    users.update(team["admin"], team["bob"].id, is_active=False)
    bob = users.get(team["bob"].id)
    with pytest.raises(NotFound):
        projects.get(bob, team["project"].slug)


def test_last_owner_protected(team):
    slug = team["project"].slug
    # admin created the project → admin1 + alice are owners
    projects.remove_member(team["alice"], slug, team["admin"].id)
    with pytest.raises(Conflict):
        projects.set_member(team["alice"], slug, "alice", "developer")
    with pytest.raises(Conflict):
        projects.remove_member(team["alice"], slug, team["alice"].id)


def test_developer_controls_only_own_runs(team):
    pid = team["project"].id
    bob, alice = team["bob"], team["alice"]
    assert rbac.can_control_run(bob, pid, bob.id)
    assert not rbac.can_control_run(bob, pid, alice.id)
    assert rbac.can_control_run(alice, pid, bob.id)
    assert not rbac.can_control_run(team["carol"], pid, team["carol"].id)


def test_list_for_only_shows_memberships(team):
    assert [p.slug for p, _ in projects.list_for(team["carol"])] == []
    assert [r for _, r in projects.list_for(team["bob"])] == ["developer"]
