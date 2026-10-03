import pytest

from sagent.core import settings, users
from sagent.core.errors import Conflict, Forbidden, ValidationError

from conftest import PASSWORD


def test_password_policy(app):
    with pytest.raises(ValidationError):
        users.create(users.SYSTEM, "dave", "short")
    with pytest.raises(ValidationError):
        users.create(users.SYSTEM, "dave", "dave-is-my-password")


def test_username_policy(app):
    for bad in ("A", "Bad Name", "../x", "x" * 40, ""):
        with pytest.raises(ValidationError):
            users.create(users.SYSTEM, bad, PASSWORD)


def test_password_hash_not_plaintext(app, make_user):
    from sagent import db

    make_user("erin")
    stored = db.scalar("SELECT password_hash FROM users WHERE username='erin'")
    assert PASSWORD not in stored and stored.startswith(("scrypt:", "pbkdf2:"))


def test_duplicate_username(app, make_user):
    make_user("frank")
    with pytest.raises(Conflict):
        make_user("frank")


def test_member_cannot_manage_users(app, make_user):
    m = make_user("gina")
    with pytest.raises(Forbidden):
        users.create(m, "hank", PASSWORD)


def test_last_admin_protected(app, make_user):
    a = make_user("root1", "admin")
    with pytest.raises(Conflict):
        users.update(a, a.id, role="member")
    with pytest.raises(Conflict):
        users.update(a, a.id, is_active=False)
    b = make_user("root2", "admin")
    users.update(a, b.id, role="member")


def test_authenticate_and_lockout(app, make_user):
    make_user("ivan")
    assert users.authenticate("ivan", PASSWORD, "203.0.113.1") is not None
    settings.put("auth.lockout_threshold", "3")
    for _ in range(3):
        assert users.authenticate("ivan", "wrong-password!", "203.0.113.2") is None
    # locked even with the right password, from the same IP
    assert users.authenticate("ivan", PASSWORD, "203.0.113.2") is None
    # another IP still works: a username lock needs 3x the threshold (limits lock-out DoS)
    assert users.authenticate("ivan", PASSWORD, "203.0.113.3") is not None
    for i in range(6):
        users.authenticate("ivan", "wrong-password!", f"203.0.113.{10 + i}")
    assert users.authenticate("ivan", PASSWORD, "203.0.113.50") is None  # distributed guessing → username lock
    users.clear_lockouts(users.SYSTEM)
    assert users.authenticate("ivan", PASSWORD, "203.0.113.3") is not None


def test_inactive_user_cannot_login(app, make_user):
    u = make_user("judy")
    users.update(users.SYSTEM, u.id, is_active=False)
    assert users.authenticate("judy", PASSWORD, "203.0.113.9") is None


def test_password_change_bumps_epoch(app, make_user):
    u = make_user("kim")
    u2 = users.change_password(u, PASSWORD, "another-long-password")
    assert u2.session_epoch == u.session_epoch + 1


def test_delete_user(app, make_user, workspace):
    from sagent import db
    from sagent.core import projects

    root = make_user("root1", "admin")
    alice = make_user("alice")
    bob = make_user("bob")
    with pytest.raises(Conflict):
        users.delete(root, root.id)  # not yourself
    with pytest.raises(Forbidden):
        users.delete(alice, bob.id)
    p = projects.create(root, "P", str(workspace / "proj1"))
    projects.set_member(root, p.slug, "alice", "owner")
    projects.remove_member(root, p.slug, root.id)  # alice is now the sole owner
    with pytest.raises(Conflict, match="유일한 owner"):
        users.delete(root, alice.id)
    projects.set_member(root, p.slug, "bob", "owner")
    users.delete(root, alice.id)
    assert users.find("alice") is None
    assert db.scalar("SELECT COUNT(*) FROM project_members WHERE user_id = ?", (alice.id,)) == 0
    assert users.authenticate("alice", PASSWORD, "203.0.113.7") is None


def test_password_min_length_is_nine(app):
    with pytest.raises(ValidationError, match="9자 이상"):
        users.create(users.SYSTEM, "nina", "Abcdefg1")       # 8 chars
    assert users.create(users.SYSTEM, "nina", "Abcdefg12")   # 9 chars
