from sagent.core import projects, settings, users

from conftest import PASSWORD


def test_login_page_without_users_shows_cli_hint(browser):
    body = browser.get("/login").get_data(as_text=True)
    assert "sagent user create-admin" in body


def test_redirects_to_login(browser):
    resp = browser.get("/")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_htmx_unauthenticated_gets_401(browser):
    resp = browser.get("/", headers={"HX-Request": "true"})
    assert resp.status_code == 401


def test_login_logout(browser, make_user):
    make_user("alice")
    resp = browser.login("alice")
    assert resp.status_code == 302
    assert browser.get("/").status_code == 200
    browser.post("/logout")
    assert browser.get("/").status_code == 302


def test_bad_login_is_generic(browser, make_user):
    make_user("alice")
    r1 = browser.post("/login", {"username": "alice", "password": "nope-nope-nope"})
    r2 = browser.post("/login", {"username": "nobody", "password": "nope-nope-nope"})
    assert r1.status_code == r2.status_code == 401


def test_csrf_required(browser, make_user):
    make_user("alice")
    browser.get("/login")
    resp = browser.post("/login", {"username": "alice", "password": PASSWORD}, csrf=False)
    assert resp.status_code == 400
    resp = browser.post("/login", {"username": "alice", "password": PASSWORD, "csrf_token": "forged"})
    assert resp.status_code == 400


def test_csrf_header_accepted(browser, make_user):
    make_user("alice")
    browser.login("alice")
    resp = browser.c.post("/logout", headers={"X-CSRF-Token": browser.csrf})
    assert resp.status_code == 302


def test_security_headers(browser):
    resp = browser.get("/login")
    csp = resp.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    cookie = resp.headers.get("Set-Cookie", "")
    assert cookie.startswith("sagent_session=") and "HttpOnly" in cookie


def test_no_inline_script_in_pages(browser, make_user):
    make_user("root", "admin")
    browser.login("root")
    for url in ("/", "/admin/users", "/admin/settings", "/admin/audit", "/projects/new"):
        body = browser.get(url).get_data(as_text=True)
        assert "<script>" not in body and "onclick=" not in body, url


def test_open_redirect_blocked(new_browser, make_user):
    make_user("alice")
    for target in ("//evil.example", "https://evil.example", "/\\evil.example"):
        b = new_browser()
        b.get("/login")
        resp = b.post(f"/login?next={target}", {"username": "alice", "password": PASSWORD})
        assert resp.headers["Location"] == "/", target
    b = new_browser()
    b.get("/login")
    resp = b.post("/login?next=/admin/users", {"username": "alice", "password": PASSWORD})
    assert resp.headers["Location"] == "/admin/users"


def test_must_change_password_flow(browser, make_user):
    make_user("newbie", must_change=True)
    browser.login("newbie")
    resp = browser.get("/")
    assert resp.status_code == 302 and "/account/password" in resp.headers["Location"]
    resp = browser.post("/account/password", {
        "current": PASSWORD, "new": "brand-new-password-1", "confirm": "brand-new-password-1",
    })
    assert resp.status_code == 302
    assert browser.get("/").status_code == 200


def test_disabling_user_kills_session(browser, make_user):
    u = make_user("alice")
    browser.login("alice")
    users.update(users.SYSTEM, u.id, is_active=False)
    assert browser.get("/").status_code == 302


def test_admin_pages_forbidden_for_member(browser, make_user):
    make_user("alice")
    browser.login("alice")
    for url in ("/admin/users", "/admin/settings", "/admin/audit"):
        assert browser.get(url).status_code == 403


def test_admin_creates_user(browser, make_user):
    make_user("root", "admin")
    browser.login("root")
    resp = browser.post("/admin/users", {"username": "bob", "password": "temporary-pass-1", "role": "member"})
    assert resp.status_code == 302
    bob = users.find("bob")
    assert bob and bob.must_change_password


def test_project_isolation_over_http(browser, new_browser, make_user, workspace):
    admin = make_user("root", "admin")
    make_user("alice")
    make_user("mallory")
    p = projects.create(admin, "Secret", str(workspace / "proj1"))
    projects.set_member(admin, p.slug, "alice", "viewer")

    m = new_browser()
    m.login("mallory")
    assert m.get(f"/p/{p.slug}").status_code == 404
    assert m.get(f"/p/{p.slug}/members").status_code == 404
    assert m.post(f"/p/{p.slug}/settings", {"name": "pwned"}).status_code == 404
    assert "Secret" not in m.get("/").get_data(as_text=True)

    a = new_browser()
    a.login("alice")
    assert a.get(f"/p/{p.slug}").status_code == 200
    # viewer cannot change settings or members
    assert a.post(f"/p/{p.slug}/settings", {"name": "x"}).status_code == 403
    assert a.post(f"/p/{p.slug}/members", {"username": "mallory", "role": "owner"}).status_code == 403
    assert projects.get(admin, p.slug)[0].name == "Secret"
    # viewer does not see the server path
    assert str(workspace) not in a.get(f"/p/{p.slug}").get_data(as_text=True)


def test_member_project_creation_respects_allowed_roots(browser, make_user, workspace, tmp_path):
    make_user("alice")
    browser.login("alice")
    # no allowed roots → cannot create
    assert browser.get("/projects/new").status_code == 302
    settings.put("projects.allowed_roots", str(workspace))
    assert browser.get("/projects/new").status_code == 302  # member creation is off by default
    settings.put("projects.member_can_create", "1")
    outside = tmp_path / "outside"
    outside.mkdir()
    resp = browser.post("/projects/new", {"name": "x", "path": str(outside)})
    assert "허용된 작업 경로 밖" in resp.get_data(as_text=True)
    # symlink escape is resolved and rejected
    link = workspace / "escape"
    link.symlink_to(outside)
    resp = browser.post("/projects/new", {"name": "x", "path": str(link)})
    assert "허용된 작업 경로 밖" in resp.get_data(as_text=True)
    resp = browser.post("/projects/new", {"name": "Mine", "path": str(workspace / "proj2")})
    assert resp.status_code == 302
    alice = users.find("alice")
    assert [(p.slug, r) for p, r in projects.list_for(alice)] == [("mine", "owner")]


def test_audit_records_actions_without_secrets(browser, make_user):
    from sagent import db

    make_user("root", "admin")
    browser.login("root")
    browser.post("/admin/users", {"username": "bob", "password": "temporary-pass-1", "role": "member"})
    rows = db.query("SELECT action, detail FROM audit_log")
    actions = {r["action"] for r in rows}
    assert {"auth.login", "user.create", "http.post"} <= actions
    assert all("temporary-pass-1" not in (r["detail"] or "") for r in rows)
