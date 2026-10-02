import json
import time

import pytest

from sagent.core import harness, loops, runs, scanner
from sagent.core.errors import ValidationError
from sagent.runtime import tmux

from conftest import PASSWORD


def _login(page, base, user):
    page.goto(base + "/login")
    page.fill("input[name=username]", user)
    page.fill("input[name=password]", PASSWORD)
    page.click("button[type=submit]")


def _wait_screen(run_id, text, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = runs._load(run_id)
        if r.tmux_session and text in tmux.capture(r.tmux_session):
            return True
        time.sleep(0.2)
    return False


def test_xterm_take_control_in_browser(team_project, live_server, browser_page):
    t = team_project
    run = runs.start_agent(t["dev"], t["project"].slug, "", mode="interactive")
    assert _wait_screen(run.id, "ready")
    page = browser_page
    _login(page, live_server, "dev1")
    page.goto(f"{live_server}/p/{t['project'].slug}/runs/{run.id}?take=1")
    page.wait_for_selector(".xterm-screen", timeout=10000)
    page.locator("#xterm-status", has_text="연결됨").wait_for(timeout=10000)
    page.locator(".xterm-helper-textarea").focus()
    page.keyboard.type("typed in browser")
    page.keyboard.press("Enter")
    assert _wait_screen(run.id, "you said: typed in browser")
    page.wait_for_timeout(300)
    page.click("#xterm-close")
    assert page.csp_errors == []
    events = [e["type"] for e in runs.list_events(t["dev"], run.id)]
    assert "terminal.attach" in events
    runs.stop(t["dev"], run.id)


def test_viewer_cannot_attach_and_other_dev_is_read_only(team_project, live_server, browser_page):
    t = team_project
    run = runs.start_agent(t["dev"], t["project"].slug, "", mode="interactive")
    assert _wait_screen(run.id, "ready")
    page = browser_page
    _login(page, live_server, "dev2")  # developer, but not the owner of this run → read-only
    page.goto(f"{live_server}/p/{t['project'].slug}/runs/{run.id}")
    assert page.locator("#xterm").get_attribute("data-readonly") == "1"
    page.click("#xterm-open")
    page.locator("#xterm-status", has_text="읽기 전용").wait_for(timeout=10000)
    page.locator(".xterm-helper-textarea").focus()
    page.keyboard.type("sneaky")
    page.keyboard.press("Enter")
    time.sleep(1.5)
    assert "sneaky" not in tmux.capture(runs._load(run.id).tmux_session)
    runs.stop(t["dev"], run.id)


def test_ws_rejects_cross_origin(team_project, live_server, browser_page):
    import simple_websocket

    t = team_project
    run = runs.start_agent(t["dev"], t["project"].slug, "", mode="interactive")
    assert _wait_screen(run.id, "ready")
    page = browser_page
    _login(page, live_server, "dev1")
    cookie = "; ".join(f"{c['name']}={c['value']}" for c in page.context.cookies())
    url = live_server.replace("http://", "ws://") + f"/p/{t['project'].slug}/runs/{run.id}/ws"
    with pytest.raises(simple_websocket.ConnectionClosed) as exc:
        ws = simple_websocket.Client.connect(url, headers={"Cookie": cookie, "Origin": "http://evil.example"})
        for _ in range(5):
            ws.receive(timeout=2)
    assert exc.value.reason == 1008
    with pytest.raises(simple_websocket.ConnectionClosed):  # no Origin at all
        ws = simple_websocket.Client.connect(url, headers={"Cookie": cookie})
        ws.receive(timeout=2)
    # simple_websocket sends "Host: 127.0.0.1" without the port, so the same-origin value is that host
    good = simple_websocket.Client.connect(url, headers={"Cookie": cookie, "Origin": "http://127.0.0.1"})
    assert good.receive(timeout=5)  # attached: tmux drew the screen (input before this is flushed)
    good.send(json.dumps({"type": "input", "data": "from ws\r"}))
    assert _wait_screen(run.id, "you said: from ws")
    good.close()
    runs.stop(t["dev"], run.id)


def test_loop_take_control_and_return(team_project):
    t = team_project
    harness.bootstrap(t["owner"], t["project"].slug, scanner.scan(t["project"].path), {}, overwrite=True)
    lr = loops.start(t["dev"], t["project"].slug, "quick", "NAP then MAKEFILE=x.txt")
    with pytest.raises(ValidationError):
        loops.take_control(t["dev"], lr.id)  # must pause first
    loops.pause(t["dev"], lr.id)
    lr = loops.wait(lr.id, timeout=30, poll=0.3)
    assert lr.status == "PAUSED"
    run = loops.take_control(t["dev"], lr.id)
    assert run.mode == "interactive" and run.loop_run_id == lr.id
    spec = json.loads((runs.run_dir(run.id) / "spec.json").read_text())
    assert "--resume" in spec["cmd"]  # continues the loop's last agent session
    assert loops._load(lr.id).status == "WAITING_USER"
    with pytest.raises(ValidationError):
        loops.resume(t["dev"], lr.id)
    lr = loops.return_to_automation(t["dev"], lr.id, rerun_from="implement", note="I fixed the config by hand")
    assert not runs._load(run.id).is_active
    lr = loops.wait(lr.id, timeout=60, poll=0.3)
    implement_runs = [r for r in loops.node_rows(lr.id) if r["node_key"] == "implement"]
    prompt = (runs.run_dir(implement_runs[-1]["run_id"]) / "prompt.md").read_text()
    assert "I fixed the config by hand" in prompt
