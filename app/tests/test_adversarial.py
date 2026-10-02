"""Hostile agent output, hostile reports and runaway loops."""
import json
import threading

import pytest
import yaml

from sagent import db
from sagent.core import harness, loopdef, loops, reviews, runs, scanner, tests


def _events(run_id):
    return db.query("SELECT type, data FROM events WHERE run_id = ? ORDER BY id", (run_id,))


def test_oversized_line_does_not_wedge_ingest(team_project):
    t = team_project
    run = runs.wait(runs.start_agent(t["dev"], t["project"].slug, "HUGELINE").id, timeout=60, poll=0.3)
    assert run.status == "SUCCESS", run.error
    assert run.input_tokens == 100  # the result after the huge line was still ingested
    assert any(e["type"] == "agent.warning" for e in _events(run.id))


def test_invalid_utf8_and_non_object_json(team_project):
    t = team_project
    run = runs.wait(runs.start_agent(t["dev"], t["project"].slug, "BADUTF8").id, timeout=30, poll=0.3)
    assert run.status == "SUCCESS" and run.input_tokens == 100


def test_garbage_usage_values_are_sanitised(team_project):
    t = team_project
    run = runs.wait(runs.start_agent(t["dev"], t["project"].slug, "BADUSAGE").id, timeout=30, poll=0.3)
    assert run.status == "SUCCESS", run.error
    assert run.input_tokens == 0 and run.output_tokens == 0
    assert run.cost_usd is None or (run.cost_usd == run.cost_usd and 0 <= run.cost_usd < 1e6)  # not NaN
    assert "/" not in run.agent_session_id  # path-like session id rejected


def test_html_in_agent_message_is_escaped(team_project, new_browser):
    t = team_project
    run = runs.wait(runs.start_agent(t["dev"], t["project"].slug, "HTMLMSG").id, timeout=30, poll=0.3)
    d = new_browser()
    d.login("dev1")
    html = d.get(f"/p/{t['project'].slug}/runs/{run.id}/events").get_data(as_text=True)
    assert "<img src=x" not in html and "&lt;img src=x" in html
    term = d.get(f"/p/{t['project'].slug}/runs/{run.id}/terminal").get_data(as_text=True)
    assert "<script>alert(2)" not in term


def test_review_flood_is_capped_and_normalised(team_project, git_project):
    t = team_project
    harness.bootstrap(t["owner"], t["project"].slug, scanner.scan(t["project"].path),
                      {"coding_agent": "claude", "review_agent": "claude"}, overwrite=True)
    (git_project / "app.py").write_text("print('FLOODREVIEW')\n")
    run = runs.wait(reviews.start(t["dev"], t["project"].slug).id, timeout=60, poll=0.3)
    rr = db.query_one("SELECT * FROM review_runs WHERE run_id = ?", (run.id,))
    issues = reviews.issues(rr["id"])
    assert len(issues) == 200
    assert {i["severity"] for i in issues} == {"medium"}  # unknown severity normalised
    assert all(len(i["reason"]) <= 4000 and i["line"] is None and i["confidence"] is None for i in issues)
    assert len(rr["summary"]) <= 4000


def test_unreachable_end_cycles_until_max_iterations(team_project):
    t = team_project
    harness.bootstrap(t["owner"], t["project"].slug, scanner.scan(t["project"].path), {}, overwrite=True)
    path = t["project"].path + "/.sagent/loops.yaml"
    data = yaml.safe_load(open(path))
    data["loops"]["spin"] = {"start": "a", "max_iterations": 2, "nodes": {
        "a": {"type": "agent", "prompt": "{task}"}, "b": {"type": "agent", "prompt": "{task}"}, "z": {"type": "end"}},
        "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]}
    open(path, "w").write(yaml.safe_dump(data))
    lr = loops.wait(loops.start(t["dev"], t["project"].slug, "spin", "go").id, timeout=90, poll=0.3)
    assert lr.status == "FAILED" and "max iterations" in lr.error


def test_node_timeout_kills_hanging_agent(team_project):
    t = team_project
    harness.bootstrap(t["owner"], t["project"].slug, scanner.scan(t["project"].path), {}, overwrite=True)
    path = t["project"].path + "/.sagent/loops.yaml"
    data = yaml.safe_load(open(path))
    data["loops"]["hang"] = {"start": "a", "nodes": {"a": {"type": "agent", "prompt": "SLEEP {task}", "timeout_sec": 2},
                                                     "z": {"type": "end"}},
                             "edges": [{"from": "a", "to": "z", "when": "success"}]}
    open(path, "w").write(yaml.safe_dump(data))
    lr = loops.wait(loops.start(t["dev"], t["project"].slug, "hang", "x").id, timeout=30, poll=0.5)
    assert lr.status == "FAILED"
    child = runs._load(loops.node_rows(lr.id)[0]["run_id"])
    assert child.status == "FAILED" and "timeout" in child.error and not child.is_active


def test_concurrent_steppers_launch_each_node_once(team_project):
    t = team_project
    harness.bootstrap(t["owner"], t["project"].slug, scanner.scan(t["project"].path), {}, overwrite=True)
    lr = loops.start(t["dev"], t["project"].slug, "quick", "NAP")
    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            runs.tick()
            loops.step(lr.id)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for th in threads:
        th.start()
    try:
        final = loops.wait(lr.id, timeout=60, poll=0.2)
    finally:
        stop.set()
        for th in threads:
            th.join()
    assert final.status in ("SUCCESS", "FAILED")
    keys = [r["node_key"] for r in loops.node_rows(lr.id)]
    assert keys.count("implement") == 1 or final.iteration > 0  # no duplicate parallel launches
    assert len(keys) == len(set((r["node_key"], r["visit"], r["attempt"]) for r in loops.node_rows(lr.id)))


def test_hostile_playwright_report(team_project, tmp_path):
    """Attachments pointing outside the run directory are never served."""
    t = team_project
    harness.bootstrap(t["owner"], t["project"].slug, scanner.scan(t["project"].path), {}, overwrite=True)
    run = tests.start_suite(t["dev"], t["project"].slug, "unit", "true")
    runs.wait(run.id, timeout=20, poll=0.3)
    tr_id = db.scalar("SELECT id FROM test_runs WHERE run_id = ?", (run.id,))
    from sagent.core.errors import NotFound

    for rel in ("/etc/passwd", "../../app.db", "playwright/../../../secret_key"):
        with pytest.raises(NotFound):
            tests.safe_artifact(t["dev"], tr_id, rel)


def test_loopdef_rejects_deep_garbage():
    assert loopdef.validate_loop("x", "not a dict")
    assert loopdef.validate_loop("x", {"nodes": {"a": "str"}, "start": "a"})
    assert loopdef.validate_loop("x", {"nodes": {"a": {"type": "agent", "retries": -1}}, "start": "a", "edges": "x"})
