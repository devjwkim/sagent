import pytest

from sagent import db
from sagent.core import harness, runs, scanner, tokens, users
from sagent.core.errors import Forbidden, ValidationError


@pytest.fixture
def api(app):
    client = app.test_client()

    def call(method, url, token=None, **kw):
        headers = kw.pop("headers", {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return getattr(client, method)(url, headers=headers, **kw)

    return call


def test_token_lifecycle(team_project):
    t = team_project
    tok, tid = tokens.create(t["dev"], "ci", "write", days=30)
    assert tok.startswith("sat_")
    stored = db.query_one("SELECT * FROM api_tokens WHERE id = ?", (tid,))
    assert tok not in stored["token_hash"] and stored["prefix"] == tok[:12]
    assert tokens.authenticate(tok)[0].id == t["dev"].id
    with pytest.raises(Forbidden):
        tokens.revoke(t["dev2"], tid)
    tokens.revoke(t["dev"], tid)
    assert tokens.authenticate(tok) is None
    with pytest.raises(ValidationError):
        tokens.create(users.SYSTEM, "x")


def test_expired_and_inactive_tokens(team_project):
    t = team_project
    tok, tid = tokens.create(t["dev"], "old", "read", days=1)
    db.execute("UPDATE api_tokens SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (tid,))
    assert tokens.authenticate(tok) is None
    tok2, _ = tokens.create(t["dev2"], "x", "read")
    users.update(t["admin"], t["dev2"].id, is_active=False)
    assert tokens.authenticate(tok2) is None


def test_api_auth_and_scopes(team_project, api, new_browser):
    t = team_project
    slug = t["project"].slug
    assert api("get", "/api/v1/me").status_code == 401
    assert api("get", "/api/v1/me", token="sat_bogus").status_code == 401  # check_secrets: allow (fake token)
    # a logged-in browser session is NOT accepted by the API (no cookie auth → no CSRF surface)
    b = new_browser()
    b.login("dev1")
    assert b.get("/api/v1/me").status_code == 401
    read, _ = tokens.create(t["dev"], "ro", "read")
    write, _ = tokens.create(t["dev"], "rw", "write")
    assert api("get", "/api/v1/me", read).json["username"] == "dev1"
    assert [p["slug"] for p in api("get", "/api/v1/projects", read).json["projects"]] == [slug]
    assert api("post", f"/api/v1/projects/{slug}/runs", read, json={"prompt": "x"}).status_code == 403
    r = api("post", f"/api/v1/projects/{slug}/runs", write, json={"prompt": "via api"})
    assert r.status_code == 201
    run_id = r.json["run"]["id"]
    runs.wait(run_id, timeout=30, poll=0.3)
    got = api("get", f"/api/v1/runs/{run_id}", read).json["run"]
    assert got["status"] == "SUCCESS" and got["input_tokens"] == 100
    assert api("get", "/api/v1/usage", read).json["totals"]["runs"] == 1


def test_api_respects_rbac(team_project, api, make_user):
    t = team_project
    slug = t["project"].slug
    eve = make_user("eve")
    etok, _ = tokens.create(eve, "e", "write")
    assert api("get", f"/api/v1/projects/{slug}/runs", etok).status_code == 404
    assert api("post", f"/api/v1/projects/{slug}/runs", etok, json={"prompt": "x"}).status_code == 404
    vtok, _ = tokens.create(t["viewer"], "v", "write")
    assert api("post", f"/api/v1/projects/{slug}/runs", vtok, json={"prompt": "x"}).status_code == 403
    assert api("get", f"/api/v1/projects/{slug}/runs", vtok).status_code == 200


def test_api_loop_flow(team_project, api):
    from sagent.core import loops

    t = team_project
    harness.bootstrap(t["owner"], t["project"].slug, scanner.scan(t["project"].path), {}, overwrite=True)
    tok, _ = tokens.create(t["dev"], "ci", "write")
    r = api("post", f"/api/v1/projects/{t['project'].slug}/loops", tok, json={"loop": "quick", "task": "MAKEFILE=a"})
    assert r.status_code == 201
    lid = r.json["loop_run"]["id"]
    loops.wait(lid, timeout=60, poll=0.3)
    body = api("get", f"/api/v1/loops/{lid}", tok).json["loop_run"]
    assert body["status"] == "SUCCESS" and [n["node"] for n in body["nodes"]] == ["implement", "unit", "done"]


def test_api_bad_token_counts_toward_lockout(team_project, api):
    from sagent.core import settings

    settings.put("auth.lockout_threshold", "3")
    for _ in range(3):
        api("get", "/api/v1/me", token="sat_wrong")  # check_secrets: allow (fake token)
    good, _ = tokens.create(team_project["dev"], "g", "read")
    assert api("get", "/api/v1/me", token=good).status_code == 401  # IP locked


def test_tokens_page(team_project, new_browser):
    d = new_browser()
    d.login("dev1")
    resp = d.post("/account/tokens", {"name": "laptop", "scope": "read", "days": "30"})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "sat_" in body
    assert "laptop" in d.get("/account/tokens").get_data(as_text=True)
