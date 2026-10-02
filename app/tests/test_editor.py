import pytest
import yaml

from sagent.core import harness, loop_editor, loops, scanner
from sagent.core.errors import Forbidden, ValidationError

from conftest import PASSWORD


def _boot(t):
    harness.bootstrap(t["owner"], t["project"].slug, scanner.scan(t["project"].path), {}, overwrite=True)


def _loops_yaml(t):
    return yaml.safe_load(open(t["project"].path + "/.sagent/loops.yaml"))


SPEC = {
    "description": "mine", "max_iterations": 3, "start": "build",
    "nodes": {"build": {"type": "agent", "prompt": "{task}", "ui": {"x": 40, "y": 30}, "retries": "1"},
              "check": {"type": "test", "suite": "unit", "ui": {"x": 300, "y": 30}},
              "fin": {"type": "end", "ui": {"x": 560, "y": 30}}},
    "edges": [{"from": "build", "to": "check"}, {"from": "check", "to": "fin", "when": "success"},
              {"from": "check", "to": "build", "when": "failure"}],
}


def test_save_and_delete_loop(team_project):
    t = team_project
    _boot(t)
    loop_editor.save(t["owner"], t["project"].slug, "mine", SPEC, make_default=True)
    data = _loops_yaml(t)
    assert data["default"] == "mine"
    assert data["loops"]["mine"]["nodes"]["build"] == {"type": "agent", "prompt": "{task}",
                                                       "ui": {"x": 40, "y": 30}, "retries": 1}
    # rename keeps default pointer
    loop_editor.save(t["owner"], t["project"].slug, "mine2", SPEC, old_name="mine")
    data = _loops_yaml(t)
    assert "mine" not in data["loops"] and data["default"] == "mine2"
    loop_editor.delete(t["owner"], t["project"].slug, "mine2")
    assert "mine2" not in _loops_yaml(t)["loops"]


def test_editor_validation_and_permissions(team_project):
    t = team_project
    _boot(t)
    slug = t["project"].slug
    with pytest.raises(Forbidden):
        loop_editor.save(t["dev"], slug, "x", SPEC)
    bad = {**SPEC, "edges": [{"from": "build", "to": "ghost"}]}
    with pytest.raises(ValidationError):
        loop_editor.save(t["owner"], slug, "x", bad)
    with pytest.raises(ValidationError):
        loop_editor.save(t["owner"], slug, "Bad Name", SPEC)
    with pytest.raises(ValidationError):
        loop_editor.save(t["owner"], slug, "x", {**SPEC, "nodes": {"<script>": {"type": "end"}}})
    with pytest.raises(ValidationError):
        loop_editor.save(t["owner"], slug, "standard", SPEC)  # exists, not a rename
    wrong = {**SPEC, "edges": SPEC["edges"] + [{"from": "check", "to": "fin", "when": "approve"}]}
    with pytest.raises(ValidationError, match="success/failure"):
        loop_editor.save(t["owner"], slug, "y", wrong)


def test_editor_http_json(team_project, new_browser):
    t = team_project
    _boot(t)
    slug = t["project"].slug
    o = new_browser()
    o.login("olivia")
    page = o.get(f"/p/{slug}/loops/editor/standard").get_data(as_text=True)
    assert 'id="loop-data"' in page and "loop-editor.js" in page
    r = o.c.post(f"/p/{slug}/loops/editor", json={"name": "mine", "loop": SPEC}, headers={"X-CSRF-Token": o.csrf})
    assert r.status_code == 200 and r.json["ok"]
    r = o.c.post(f"/p/{slug}/loops/editor", json={"name": "mine", "loop": SPEC})
    assert r.status_code == 400  # CSRF required for JSON too
    d = new_browser()
    d.login("dev1")
    assert d.get(f"/p/{slug}/loops/editor").status_code == 403
    r = d.c.post(f"/p/{slug}/loops/editor", json={"name": "z", "loop": SPEC}, headers={"X-CSRF-Token": d.csrf})
    assert r.status_code == 403
    # saved positions are used by the runtime graph
    lr = loops.start(t["dev"], slug, "mine", "MAKEFILE=a.txt")
    page = d.get(f"/p/{slug}/loops/runs/{lr.id}").get_data(as_text=True)
    assert 'x="40" y="30"' in page
    loops.stop(t["dev"], lr.id)


def test_editor_in_real_browser(team_project, live_server, browser_page):
    t = team_project
    _boot(t)
    slug = t["project"].slug
    page = browser_page
    page.goto(live_server + "/login")
    page.fill("input[name=username]", "olivia")
    page.fill("input[name=password]", PASSWORD)
    page.click("button[type=submit]")
    page.goto(f"{live_server}/p/{slug}/loops/editor")
    page.wait_for_selector("svg.editor-canvas g.node")
    assert page.locator("svg.editor-canvas g.node").count() == 3  # quick template
    page.click("text=+ review")
    review = page.locator('svg.editor-canvas g.node[data-key="review"]')
    box = review.bounding_box()
    page.mouse.move(box["x"] + 20, box["y"] + 20)
    page.mouse.down()
    page.mouse.move(box["x"] + 420, box["y"] + 200, steps=5)
    page.mouse.up()
    # connect unit → review with condition success
    page.locator('svg.editor-canvas g.node[data-key="unit"]').click()
    page.click("text=연결 시작")
    page.locator('svg.editor-canvas g.node[data-key="review"]').click()
    page.locator('svg.editor-canvas g.node[data-key="unit"]').click()
    page.locator(".editor-node select").last.select_option("failure")  # last out-edge of unit
    page.fill(".editor-panel input >> nth=0", "fancy")
    page.locator(".editor-panel input >> nth=0").press("Tab")
    page.click(".editor-save")
    page.wait_for_url(f"**/p/{slug}/loops")
    data = _loops_yaml(t)
    fancy = data["loops"]["fancy"]
    assert "review" in fancy["nodes"] and fancy["nodes"]["review"]["ui"]["x"] > 300
    assert {"from": "unit", "to": "review", "when": "failure"} in fancy["edges"]
    assert page.csp_errors == []
