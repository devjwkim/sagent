import json
import subprocess

import pytest
import yaml

from sagent.core import harness, loopdef, projects, scanner
from sagent.core.errors import Forbidden, ValidationError


@pytest.fixture
def node_project(workspace):
    root = workspace / "proj1"
    (root / "package.json").write_text(json.dumps({
        "scripts": {"test": "vitest run", "lint": "eslint ."},
        "dependencies": {"next": "14", "react": "18"},
        "devDependencies": {"vitest": "1", "@playwright/test": "1", "typescript": "5"},
    }))
    (root / "tsconfig.json").write_text("{}")
    (root / "pnpm-lock.yaml").write_text("")
    (root / "README.md").write_text("# hi")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("on: push")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def test_scan_node_project(node_project):
    r = scanner.scan(node_project)
    assert r["languages"] == ["TypeScript"]
    assert "Next.js" in r["frameworks"] and "React" in r["frameworks"]
    assert r["unit_test"] == "Vitest" and r["e2e"] == "Playwright"
    assert r["commands"] == {
        "unit": "pnpm test", "lint": "pnpm lint",
        "typecheck": "npx tsc --noEmit", "e2e": "npx playwright test",
    }
    assert r["git"]["detected"] and r["ci"] == ["GitHub Actions"] and r["readme"] == "README.md"


def test_scan_python_project(workspace):
    root = workspace / "proj2"
    (root / "pyproject.toml").write_text(
        '[project]\nname="x"\ndependencies=["fastapi"]\n[project.optional-dependencies]\ndev=["pytest","ruff"]\n'
        "[tool.mypy]\n"
    )
    r = scanner.scan(root)
    assert r["languages"] == ["Python"] and r["frameworks"] == ["FastAPI"]
    assert r["commands"]["unit"] == "pytest -q"
    assert r["commands"]["lint"] == "ruff check ."
    assert r["commands"]["typecheck"] == "mypy ."


def test_scan_does_not_follow_symlink_out(workspace, tmp_path):
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "package.json").write_text('{"dependencies": {"next": "1"}}')
    (workspace / "proj2" / "package.json").symlink_to(outside / "package.json")
    assert scanner.scan(workspace / "proj2")["frameworks"] == []


@pytest.mark.parametrize("name", list(loopdef.TEMPLATES))
def test_loop_templates_are_valid(name):
    assert loopdef.validate_loop(name, loopdef.template(name)) == []


def test_loop_validation_catches_errors():
    bad = {"start": "x", "nodes": {"a": {"type": "agent"}, "b": {"type": "test", "suite": "nope"}},
           "edges": [{"from": "a", "to": "zzz", "when": "maybe"}]}
    errs = loopdef.validate_loop("bad", bad)
    joined = " ".join(errs)
    assert "start" in joined and "suite" in joined and "unknown node" in joined
    assert "when" in joined and "'end'" in joined


@pytest.fixture
def owner_project(app, make_user, node_project):
    owner = make_user("olivia")
    from sagent.core import settings

    settings.put("projects.allowed_roots", str(node_project.parent))
    settings.put("projects.member_can_create", "1")
    p = projects.create(owner, "Web App", str(node_project))
    return owner, p


def test_bootstrap_writes_config_and_agent_docs(owner_project, node_project):
    owner, p = owner_project
    scan = scanner.scan(node_project)
    res = harness.bootstrap(owner, p.slug, scan, {"coding_agent": "claude", "review_agent": "codex", "loop": "strict"})
    assert set(res["written"]) == set(harness.FILES) | {"CLAUDE.md", "AGENTS.md"}
    cfg = harness.load(p)
    assert cfg["loops.yaml"]["default"] == "strict"
    assert cfg["harness.yaml"]["agents"]["review"]["provider"] == "codex"
    assert cfg["tests.yaml"]["unit"]["command"] == "pnpm test"
    for name, text in harness.read_all(p).items():
        harness.parse(name, text)  # round-trip validates
    assert "pnpm test" in (node_project / "CLAUDE.md").read_text()
    assert projects.get(owner, p.slug)[0].lifecycle == "DEVELOPMENT"


def test_bootstrap_keeps_existing_files(owner_project, node_project):
    owner, p = owner_project
    (node_project / "CLAUDE.md").write_text("mine")
    harness.bootstrap(owner, p.slug, scanner.scan(node_project), {})
    (node_project / ".sagent" / "tests.yaml").write_text("unit: {command: custom}\n")
    res = harness.bootstrap(owner, p.slug, scanner.scan(node_project), {})
    assert res["written"] == [] or res["written"] == ["AGENTS.md"]
    assert "tests.yaml" in res["skipped"]
    assert (node_project / "CLAUDE.md").read_text() == "mine"
    assert res["docs"]["CLAUDE.md"]["status"] == "exists"


def test_save_file_validates_and_checks_role(owner_project, make_user, node_project):
    owner, p = owner_project
    harness.bootstrap(owner, p.slug, scanner.scan(node_project), {})
    dev = make_user("dan")
    projects.set_member(owner, p.slug, "dan", "developer")
    good = harness.read_all(p)["review.yaml"]
    with pytest.raises(Forbidden):
        harness.save_file(dev, p.slug, "review.yaml", good)
    with pytest.raises(ValidationError):
        harness.save_file(owner, p.slug, "review.yaml", "provider: gpt\n")
    with pytest.raises(ValidationError):
        harness.save_file(owner, p.slug, "review.yaml", "a: [unclosed\n")
    with pytest.raises(ValidationError):
        harness.save_file(owner, p.slug, "../../etc/passwd", "x: 1\n")
    harness.save_file(owner, p.slug, "review.yaml", good.replace("high", "medium"))
    assert "medium" in harness.read_all(p)["review.yaml"]


def test_symlinked_config_dir_refused(owner_project, node_project, tmp_path):
    owner, p = owner_project
    target = tmp_path / "elsewhere"
    target.mkdir()
    (node_project / ".sagent").symlink_to(target)
    with pytest.raises(Forbidden):
        harness.bootstrap(owner, p.slug, scanner.scan(node_project), {})
    assert list(target.iterdir()) == []


def test_bootstrap_http_flow(browser, make_user, node_project):
    from sagent.core import settings

    make_user("olivia")
    settings.put("projects.allowed_roots", str(node_project.parent))
    settings.put("projects.member_can_create", "1")
    browser.login("olivia")
    resp = browser.post("/projects/new", {"name": "Web App", "path": str(node_project)})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/p/web-app/bootstrap")
    page = browser.get("/p/web-app/bootstrap").get_data(as_text=True)
    assert "Next.js" in page and "pnpm test" in page
    resp = browser.post("/p/web-app/bootstrap", {
        "coding_agent": "claude", "review_agent": "codex", "loop": "autofix",
        "cmd_unit": "pnpm test", "browsers": "chromium",
    })
    assert resp.status_code == 200 and "Bootstrap 완료" in resp.get_data(as_text=True)
    data = yaml.safe_load((node_project / ".sagent" / "loops.yaml").read_text())
    assert data["default"] == "autofix"
    assert browser.get("/p/web-app/harness?file=loops.yaml").status_code == 200


def test_viewer_cannot_edit_harness_over_http(browser, new_browser, make_user, node_project):
    from sagent.core import settings

    owner = make_user("olivia")
    make_user("val")
    settings.put("projects.allowed_roots", str(node_project.parent))
    settings.put("projects.member_can_create", "1")
    p = projects.create(owner, "Web App", str(node_project))
    harness.bootstrap(owner, p.slug, scanner.scan(node_project), {})
    projects.set_member(owner, p.slug, "val", "viewer")
    before = (node_project / ".sagent" / "tests.yaml").read_text()
    v = new_browser()
    v.login("val")
    assert v.get(f"/p/{p.slug}/harness").status_code == 200
    assert v.get(f"/p/{p.slug}/bootstrap").status_code == 403
    assert v.post(f"/p/{p.slug}/harness/tests.yaml", {"content": "unit: {command: 'rm -rf /'}"}).status_code == 403
    assert (node_project / ".sagent" / "tests.yaml").read_text() == before


def test_cli_import(app, node_project, capsys):
    from sagent import cli

    home = app.config["SAGENT"].home
    rc = cli.main(["--home", str(home), "import", str(node_project), "-y", "--agent", "codex", "--loop", "quick"])
    assert rc == 0
    assert (node_project / ".sagent" / "harness.yaml").exists()
    assert (node_project / "AGENTS.md").exists()
    out = capsys.readouterr().out
    assert "ready" in out
    p = projects.find_by_path(str(node_project))
    assert p.primary_agent == "codex"
