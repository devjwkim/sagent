import pytest
import yaml

from sagent import db
from sagent.core import harness, loopdef, loops, scanner
from sagent.core.errors import Forbidden, ValidationError
from sagent.web.graph import layout


def _setup(project, owner, loop_name="standard", unit="", extra_loops=None):
    harness.bootstrap(owner, project.slug, scanner.scan(project.path), {"loop": loop_name}, overwrite=True)
    base = project.path + "/.sagent/"
    tests = yaml.safe_load(open(base + "tests.yaml"))
    tests["unit"]["command"] = unit
    open(base + "tests.yaml", "w").write(yaml.safe_dump(tests))
    if extra_loops:
        data = yaml.safe_load(open(base + "loops.yaml"))
        data["loops"].update(extra_loops)
        open(base + "loops.yaml", "w").write(yaml.safe_dump(data))


def _node_seq(lr_id):
    return [(r["node_key"], r["status"]) for r in loops.node_rows(lr_id)]


def test_quick_loop_success(team_project):
    t = team_project
    _setup(t["project"], t["owner"], unit="test -f ok.txt")
    lr = loops.start(t["dev"], t["project"].slug, "quick", "MAKEFILE=ok.txt please")
    lr = loops.wait(lr.id, timeout=60, poll=0.3)
    assert lr.status == "SUCCESS", lr.error
    assert _node_seq(lr.id) == [("implement", "SUCCESS"), ("unit", "SUCCESS"), ("done", "SUCCESS")]


def test_autofix_loop_passes_failure_context(team_project, git_project):
    t = team_project
    _setup(t["project"], t["owner"], unit="test -f fixed.txt")
    lr = loops.start(t["dev"], t["project"].slug, "autofix", "FIXFILE=fixed.txt")
    lr = loops.wait(lr.id, timeout=90, poll=0.3)
    assert lr.status == "SUCCESS", lr.error
    seq = [k for k, _ in _node_seq(lr.id)]
    assert seq == ["implement", "unit", "fix", "unit", "review", "done"]
    assert lr.iteration == 1
    fix_run = db.scalar("SELECT run_id FROM loop_run_nodes WHERE loop_run_id=? AND node_key='fix'", (lr.id,))
    prompt = open(f"{__import__('sagent.core.runs', fromlist=['x']).run_dir(fix_run)}/prompt.md").read()
    assert "Previous verification failed" in prompt and "test -f fixed.txt" in prompt
    assert ("review", "SUCCESS") in _node_seq(lr.id)


def test_max_iterations(team_project):
    t = team_project
    loop = loopdef.template("quick")
    loop["max_iterations"] = 2
    _setup(t["project"], t["owner"], unit="false", extra_loops={"tight": loop})
    lr = loops.wait(loops.start(t["dev"], t["project"].slug, "tight", "do it").id, timeout=90, poll=0.3)
    assert lr.status == "FAILED" and "max iterations" in lr.error
    assert [k for k, _ in _node_seq(lr.id)].count("implement") == 3


def test_missing_command_skips_test_node(team_project):
    t = team_project
    _setup(t["project"], t["owner"], unit="")
    lr = loops.wait(loops.start(t["dev"], t["project"].slug, "quick", "x").id, timeout=60, poll=0.3)
    assert lr.status == "SUCCESS"
    assert ("unit", "SKIPPED") in _node_seq(lr.id)


def test_no_edge_fails_then_retry_succeeds(team_project, workspace):
    t = team_project
    loop = {
        "start": "implement", "max_iterations": 3,
        "nodes": {"implement": {"type": "agent", "prompt": "{task}"},
                  "unit": {"type": "test", "suite": "unit"}, "done": {"type": "end"}},
        "edges": [{"from": "implement", "to": "unit"}, {"from": "unit", "to": "done", "when": "success"}],
    }
    _setup(t["project"], t["owner"], unit="test -f later.txt", extra_loops={"strictish": loop})
    lr = loops.wait(loops.start(t["dev"], t["project"].slug, "strictish", "x").id, timeout=60, poll=0.3)
    assert lr.status == "FAILED" and "no edge" in lr.error and lr.current_node == "unit"
    with pytest.raises(Forbidden):
        loops.retry_node(t["dev2"], lr.id)
    (workspace / "proj1" / "later.txt").write_text("now")
    loops.retry_node(t["dev"], lr.id)
    lr = loops.wait(lr.id, timeout=60, poll=0.3)
    assert lr.status == "SUCCESS"
    units = [r for r in loops.node_rows(lr.id) if r["node_key"] == "unit"]
    assert [u["visit"] for u in units] == [1, 1] and lr.iteration == 0


def test_pause_resume_and_skip(team_project):
    t = team_project
    _setup(t["project"], t["owner"], unit="false")
    lr = loops.start(t["dev"], t["project"].slug, "quick", "NAP a little")
    lr = loops.pause(t["dev"], lr.id)
    lr = loops.wait(lr.id, timeout=30, poll=0.3)
    assert lr.status == "PAUSED" and lr.current_node == "unit"
    lr = loops.skip_node(t["owner"], lr.id)  # owner may control dev's loop; unit → skipped(success)
    lr = loops.wait(lr.id, timeout=30, poll=0.3)
    assert lr.status == "SUCCESS"
    assert ("unit", "SKIPPED") in _node_seq(lr.id)


def test_stop_loop_cancels_child(team_project):
    t = team_project
    _setup(t["project"], t["owner"], unit="true")
    lr = loops.start(t["dev"], t["project"].slug, "quick", "SLEEP forever")
    lr = loops.stop(t["dev"], lr.id)
    assert lr.status == "CANCELLED"
    run_id = loops.node_rows(lr.id)[0]["run_id"]
    assert db.scalar("SELECT status FROM runs WHERE id = ?", (run_id,)) == "CANCELLED"


def test_loop_permissions_and_validation(team_project):
    t = team_project
    _setup(t["project"], t["owner"])
    with pytest.raises(Forbidden):
        loops.start(t["viewer"], t["project"].slug, "quick", "x")
    with pytest.raises(ValidationError):
        loops.start(t["dev"], t["project"].slug, "nope", "x")
    with pytest.raises(ValidationError):
        loops.start(t["dev"], t["project"].slug, "quick", "  ")


def test_inactive_owner_stops_loop(team_project):
    from sagent.core import users

    t = team_project
    _setup(t["project"], t["owner"], unit="test -f never.txt")
    lr = loops.start(t["dev"], t["project"].slug, "quick", "NAP")
    users.update(t["admin"], t["dev"].id, is_active=False)
    lr = loops.wait(lr.id, timeout=30, poll=0.3)
    assert lr.status == "FAILED"


def test_graph_layout():
    g = layout(loopdef.template("standard"))
    xs = {n["key"]: n["x"] for n in g["nodes"]}
    assert xs["plan"] < xs["implement"] < xs["unit"] < xs["review"] < xs["done"]
    backs = [e for e in g["edges"] if e["back"]]
    assert len(backs) == 2 and {e["when"] for e in backs} == {"failure", "reject"}


def test_loop_pages(team_project, new_browser):
    t = team_project
    _setup(t["project"], t["owner"], unit="true")
    slug = t["project"].slug
    d = new_browser()
    d.login("dev1")
    page = d.get(f"/p/{slug}/loops").get_data(as_text=True)
    assert "<svg" in page and "standard" in page
    resp = d.post(f"/p/{slug}/loops", {"loop": "quick", "task": "MAKEFILE=a.txt"})
    assert resp.status_code == 302
    lr_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    loops.wait(lr_id, timeout=60, poll=0.3)
    assert "SUCCESS" in d.get(f"/p/{slug}/loops/runs/{lr_id}").get_data(as_text=True)
    assert d.get(f"/p/{slug}/loops/runs/{lr_id}/live").status_code == 286
    o = new_browser()
    o.login("dev2")
    assert o.post(f"/p/{slug}/loops/runs/{lr_id}/control", {"action": "rerun_from", "node": "unit"}).status_code == 403
