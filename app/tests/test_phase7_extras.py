import json

import pytest
import yaml

from sagent import db
from sagent.core import harness, loop_library, runs, scanner, scenarios, tests
from sagent.core.errors import Conflict, Forbidden, ValidationError


def _boot(t):
    harness.bootstrap(t["owner"], t["project"].slug, scanner.scan(t["project"].path), {}, overwrite=True)


def test_detect_pages(workspace):
    root = workspace / "proj2"
    for rel in ("app/page.tsx", "app/(shop)/cart/page.tsx", "app/products/[id]/page.tsx", "pages/settings.tsx",
                "pages/api/x.ts", "pages/_app.tsx", "node_modules/lib/app/page.tsx"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    (root / "server.py").write_text(
        """
@app.route("/login")
def login():
    pass


@bp.get("/admin/users")
def users():
    pass
""")
    (root / "srv.js").write_text("app.get('/health', h)\n")
    pages = scenarios.detect_pages(str(root))
    assert pages == sorted(["/", "/cart", "/products/[id]", "/settings", "/login", "/admin/users", "/health"])


def test_wizard_starts_agent_and_enables_e2e(team_project):
    t = team_project
    _boot(t)
    run = scenarios.start(t["owner"], t["project"].slug, pages=["/login"], scenarios=["Login", "Checkout"],
                          browsers=["chromium", "firefox"], mobile=True, visual="critical",
                          base_url="http://localhost:3000")
    prompt = (runs.run_dir(run.id) / "prompt.md").read_text()
    assert "- Login" in prompt and "/login" in prompt and "toHaveScreenshot" in prompt and "mobile viewport" in prompt
    assert "Do not change application code" in prompt
    e2e = yaml.safe_load(open(t["project"].path + "/.sagent/tests.yaml"))["e2e"]
    assert e2e["enabled"] and e2e["browsers"] == ["chromium", "firefox"] and e2e["visual_regression"] == "critical"
    runs.wait(run.id, timeout=30, poll=0.3)
    with pytest.raises(ValidationError):
        scenarios.start(t["dev"], t["project"].slug, pages=[], scenarios=[], browsers=[], mobile=False)
    with pytest.raises(ValidationError):
        scenarios.start(t["dev"], t["project"].slug, pages=[], scenarios=["x"], browsers=[], mobile=False,
                        base_url="javascript:alert(1)")
    with pytest.raises(Forbidden):
        scenarios.start(t["viewer"], t["project"].slug, pages=[], scenarios=["x"], browsers=[], mobile=False)


def test_wizard_page(team_project, new_browser):
    t = team_project
    _boot(t)
    d = new_browser()
    d.login("dev1")
    assert "E2E 시나리오 마법사" in d.get(f"/p/{t['project'].slug}/tests/wizard").get_data(as_text=True)
    v = new_browser()
    v.login("vic")
    assert v.get(f"/p/{t['project'].slug}/tests/wizard").status_code == 403


def test_update_snapshots(team_project):
    t = team_project
    _boot(t)
    p = t["project"].path + "/.sagent/tests.yaml"
    data = yaml.safe_load(open(p))
    data["e2e"].update(enabled=True, command="npx playwright test")
    open(p, "w").write(yaml.safe_dump(data))
    run = tests.start_suite(t["dev"], t["project"].slug, "e2e")
    runs.wait(run.id, timeout=30, poll=0.3)
    tid = db.scalar("SELECT id FROM test_runs WHERE run_id = ?", (run.id,))
    with pytest.raises(Forbidden):
        tests.update_snapshots(t["dev"], tid)
    run2 = tests.update_snapshots(t["owner"], tid)
    assert "--update-snapshots" in run2.command
    runs.wait(run2.id, timeout=30, poll=0.3)


def test_loop_library(team_project, make_user, workspace):
    from sagent.core import projects

    t = team_project
    _boot(t)
    slug = t["project"].slug
    with pytest.raises(Forbidden):
        loop_library.share(t["dev"], slug, "standard", "team-std")
    with pytest.raises(ValidationError):
        loop_library.share(t["owner"], slug, "standard", "quick")  # clashes with built-in
    tid = loop_library.share(t["owner"], slug, "strict", "team-strict", "our strict flow")
    with pytest.raises(Conflict):
        loop_library.share(t["owner"], slug, "strict", "team-strict")
    names = [x["name"] for x in loop_library.list_all()]
    assert "team-strict" in names and "standard" in names
    # another project picks it up
    other = projects.create(t["admin"], "Other", str(workspace / "proj2"))
    harness.bootstrap(t["admin"], other.slug, scanner.scan(other.path), {})
    assert loop_library.add_to_project(t["admin"], other.slug, "team-strict") == "team-strict"
    assert "team-strict" in yaml.safe_load(open(other.path + "/.sagent/loops.yaml"))["loops"]
    with pytest.raises(Forbidden):
        loop_library.delete(t["dev"], tid)
    loop_library.delete(t["owner"], tid)
    assert "team-strict" not in [x["name"] for x in loop_library.list_all()]


def test_library_page(team_project, new_browser):
    t = team_project
    _boot(t)
    slug = t["project"].slug
    o = new_browser()
    o.login("olivia")
    assert "autofix" in o.get(f"/p/{slug}/loops/library").get_data(as_text=True)
    assert o.post(f"/p/{slug}/loops/library", {"action": "add", "template": "autofix", "as_name": "af2"}).status_code == 302
    assert "af2" in yaml.safe_load(open(t["project"].path + "/.sagent/loops.yaml"))["loops"]
    d = new_browser()
    d.login("dev1")
    assert d.post(f"/p/{slug}/loops/library", {"action": "add", "template": "autofix"}).status_code == 403
