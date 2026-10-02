import sys

import pytest
import yaml

from sagent import db
from sagent.core import harness, loops, runs, scanner, tests
from sagent.core.errors import NotFound


def _set_cmd(project, owner, suite, command, **extra):
    harness.bootstrap(owner, project.slug, scanner.scan(project.path), {}, overwrite=True)
    p = project.path + "/.sagent/tests.yaml"
    data = yaml.safe_load(open(p))
    data[suite]["command"] = command
    data[suite].update(extra)
    open(p, "w").write(yaml.safe_dump(data))


def _wait_test(run):
    runs.wait(run.id, timeout=60, poll=0.3)
    tid = db.scalar("SELECT id FROM test_runs WHERE run_id = ?", (run.id,))
    return tests.TestRun.from_row(db.query_one("SELECT * FROM test_runs WHERE id = ?", (tid,)))


def test_pytest_suite_with_junit(team_project, workspace):
    t = team_project
    root = workspace / "proj1"
    (root / "tests").mkdir()
    (root / "tests" / "test_sample.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n\n\ndef test_bad():\n    assert 'a' == 'b', 'letters differ'\n"
    )
    _set_cmd(t["project"], t["owner"], "unit", f"{sys.executable} -m pytest -q -p no:cacheprovider tests")
    tr = _wait_test(tests.start_suite(t["dev"], t["project"].slug, "unit"))
    assert tr.status == "FAILED" and tr.framework == "pytest"
    assert (tr.total, tr.passed, tr.failed) == (2, 1, 1)
    failed = [c for c in tests.cases(tr.id) if c["status"] == "failed"]
    assert failed[0]["title"] == "test_bad" and "letters differ" in failed[0]["error"]


def test_playwright_suite(team_project, new_browser):
    t = team_project
    _set_cmd(t["project"], t["owner"], "e2e", "npx playwright test", enabled=True)
    tr = _wait_test(tests.start_suite(t["dev"], t["project"].slug, "e2e"))
    assert tr.framework == "playwright" and tr.status == "FAILED" and tr.has_report == 1
    assert (tr.total, tr.passed, tr.failed) == (3, 2, 1)
    bad = [c for c in tests.cases(tr.id) if c["status"] == "failed"][0]
    assert bad["title"] == "checkout pays" and "expected 'Paid'" in bad["error"]
    assert "Open payment" in bad["steps"]

    slug = t["project"].slug
    d = new_browser()
    d.login("dev1")
    page = d.get(f"/p/{slug}/tests/{tr.id}").get_data(as_text=True)
    assert "checkout pays" in page and "Playwright HTML Report" in page and "<img" in page
    rep = d.get(f"/p/{slug}/tests/{tr.id}/report/")
    assert rep.status_code == 200 and "Playwright report" in rep.get_data(as_text=True)
    assert rep.headers["Content-Security-Policy"].startswith("sandbox ")
    shot = d.get(f"/p/{slug}/tests/{tr.id}/file/playwright/test-results/checkout-failed-1.png")
    assert shot.status_code == 200 and shot.mimetype == "image/png"
    # path traversal out of the test output dir
    for evil in ("../app.db", "playwright/../../app.db", "..%2f..%2fapp.db"):
        assert d.get(f"/p/{slug}/tests/{tr.id}/file/{evil}").status_code == 404
    v = new_browser()
    v.login("vic")
    assert v.get(f"/p/{slug}/tests/{tr.id}").status_code == 200
    assert v.get(f"/p/{slug}/tests/{tr.id}/report/").status_code == 403


def test_safe_artifact_rejects_symlink_escape(team_project, tmp_path):
    t = team_project
    _set_cmd(t["project"], t["owner"], "unit", "true")
    tr = _wait_test(tests.start_suite(t["dev"], t["project"].slug, "unit"))
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    (tests.output_dir(tr.id) / "link.txt").symlink_to(secret)
    with pytest.raises(NotFound):
        tests.safe_artifact(t["dev"], tr.id, "link.txt")


def test_junit_entities_refused():
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><testsuite><testcase name="&a;"/></testsuite>'
    assert tests.parse_junit(evil) == []


def test_ai_failure_analysis(team_project):
    t = team_project
    _set_cmd(t["project"], t["owner"], "e2e", "npx playwright test", enabled=True)
    tr = _wait_test(tests.start_suite(t["dev"], t["project"].slug, "e2e"))
    run = tests.analyze_failures(t["dev"], tr.id)
    assert run.role == "test_analysis"
    prompt = (runs.run_dir(run.id) / "prompt.md").read_text()
    assert "checkout pays" in prompt and "Do NOT modify" in prompt
    runs.wait(run.id, timeout=30, poll=0.3)


def test_loop_test_node_records_test_run(team_project):
    t = team_project
    _set_cmd(t["project"], t["owner"], "unit", "true")
    lr = loops.wait(loops.start(t["dev"], t["project"].slug, "quick", "x").id, timeout=60, poll=0.3)
    assert lr.status == "SUCCESS"
    row = db.query_one("SELECT * FROM test_runs WHERE loop_run_id = ?", (lr.id,))
    assert row["suite"] == "unit" and row["status"] == "SUCCESS"


def test_tests_page(team_project, new_browser):
    t = team_project
    _set_cmd(t["project"], t["owner"], "unit", "true")
    d = new_browser()
    d.login("dev1")
    slug = t["project"].slug
    assert "true" in d.get(f"/p/{slug}/tests").get_data(as_text=True)
    resp = d.post(f"/p/{slug}/tests", {"suite": "unit"})
    assert resp.status_code == 302 and "/tests/" in resp.headers["Location"]
    v = new_browser()
    v.login("vic")
    assert v.post(f"/p/{slug}/tests", {"suite": "unit"}).status_code == 403
