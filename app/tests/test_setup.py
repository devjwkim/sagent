import os
import re

from sagent.core import setup, users


def _token(app):
    return setup.ensure_token(app.config["SAGENT"].home)


def test_setup_page_requires_token(app, browser):
    tok = _token(app)
    assert browser.get("/setup").status_code == 404
    assert browser.get("/setup?token=wrong").status_code == 302  # stores it, then…
    assert browser.get("/setup").status_code == 404              # …rejects it
    path = app.config["SAGENT"].home / "setup_token"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600" and tok


def test_setup_creates_admin_once(app, new_browser):
    tok = _token(app)
    b = new_browser()
    r = b.get(f"/setup?token={tok}")
    assert r.status_code == 302 and r.headers["Location"].endswith("/setup")  # token removed from URL
    page = b.get("/setup").get_data(as_text=True)
    assert "첫 관리자 만들기" in page and tok not in page
    r = b.post("/setup", {"username": "boss", "password": "First-Admin-Pass1", "confirm": "First-Admin-Pass1"})
    assert r.status_code == 302 and "/admin/settings" in r.headers["Location"]
    assert users.find("boss").is_admin
    assert b.get("/admin/settings").status_code == 200  # logged in
    assert not (app.config["SAGENT"].home / "setup_token").exists()
    other = new_browser()
    assert other.get(f"/setup?token={tok}").status_code == 404  # gone for good
    assert setup.ensure_token(app.config["SAGENT"].home) is None


def test_setup_validation_errors(app, new_browser):
    tok = _token(app)
    b = new_browser()
    b.get(f"/setup?token={tok}")
    b.get("/setup")
    r = b.post("/setup", {"username": "boss", "password": "short", "confirm": "short"})
    assert "9자 이상" in r.get_data(as_text=True)
    r = b.post("/setup", {"username": "boss", "password": "First-Admin-Pass1", "confirm": "different-pass-1"})
    assert "일치하지 않습니다" in r.get_data(as_text=True)
    assert users.count_users() == 0


def test_setup_closed_when_users_exist(app, make_user, browser):
    make_user("root", "admin")
    assert setup.ensure_token(app.config["SAGENT"].home) is None
    assert browser.get("/setup?token=anything").status_code == 404


def test_cli_setup_url(app, capsys):
    from sagent import cli

    home = str(app.config["SAGENT"].home)
    assert cli.main(["--home", home, "setup-url", "--port", "17832"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"http://127\.0\.0\.1:17832/setup\?token=[\w-]{20,}", out)
