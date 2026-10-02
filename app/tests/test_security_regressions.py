"""Regression tests for the security audit findings."""
import json
import subprocess
import time

import pytest

from sagent import db
from sagent.core import projects, runs
from sagent.core.errors import Conflict, NotFound, ValidationError
from sagent.runtime import tmux

from conftest import PASSWORD


def test_tmux_has_no_key_bindings(team_project):
    t = team_project
    run = runs.start_agent(t["dev"], t["project"].slug, "", mode="interactive")
    out = subprocess.run(["tmux", "-L", tmux.socket_name(), "list-keys"], capture_output=True, text=True)
    assert out.stdout.strip() == ""  # no prefix/choose-tree/command-prompt reachable from a pane
    prefix = subprocess.run(["tmux", "-L", tmux.socket_name(), "show-options", "-gv", "prefix"],
                            capture_output=True, text=True).stdout.strip()
    assert prefix == "None"
    runs.stop(t["dev"], run.id)


def test_resume_id_cannot_be_an_option(team_project):
    t = team_project
    for bad in ("--dangerously-skip-permissions", "-x", "_x"):
        with pytest.raises(ValidationError):
            runs.start_agent(t["dev"], t["project"].slug, "x", resume_session=bad)


def test_interactive_prompt_is_not_an_option(team_project):
    t = team_project
    run = runs.start_agent(t["dev"], t["project"].slug, "--dangerously-skip-permissions", mode="interactive")
    cmd = json.loads((runs.run_dir(run.id) / "spec.json").read_text())["cmd"]
    assert cmd[-2:] == ["--", "--dangerously-skip-permissions"]
    runs.stop(t["dev"], run.id)


def test_nested_project_paths_rejected(team_project, workspace):
    t = team_project
    (workspace / "proj1" / "sub").mkdir()
    with pytest.raises(Conflict):
        projects.create(t["admin"], "Inner", str(workspace / "proj1" / "sub"))
    with pytest.raises(Conflict):
        projects.create(t["admin"], "Outer", str(workspace))


def test_audit_has_no_typed_text(team_project):
    t = team_project
    run = runs.start_agent(t["dev"], t["project"].slug, "", mode="interactive")
    time.sleep(0.5)
    runs.send_input(t["dev"], run.id, "my-secret-token-123")
    rows = db.query("SELECT detail FROM audit_log WHERE action = 'run.input'")
    assert rows and all("my-secret-token-123" not in r["detail"] for r in rows)
    runs.stop(t["dev"], run.id)


def test_uniform_not_found_messages(team_project, make_user):
    t = team_project
    run = runs.wait(runs.start_agent(t["dev"], t["project"].slug, "hi").id, timeout=30, poll=0.3)
    eve = make_user("eve")
    with pytest.raises(NotFound) as hidden:
        runs.get(eve, run.id)
    with pytest.raises(NotFound) as missing:
        runs.get(eve, 99999)
    assert str(hidden.value) == str(missing.value)


def test_member_picker_hides_directory_from_non_admin(team_project, new_browser, make_user):
    t = team_project
    make_user("outsider-zed")
    o = new_browser()
    o.login("olivia")
    page = o.get(f"/p/{t['project'].slug}/members").get_data(as_text=True)
    assert "outsider-zed" not in page and 'name="username" required maxlength' in page
    a = new_browser()
    a.login("admin1")
    assert "outsider-zed" in a.get(f"/p/{t['project'].slug}/members").get_data(as_text=True)


def test_revoked_user_loses_live_terminal(team_project, live_server, browser_page):
    import simple_websocket

    from sagent.core import users

    t = team_project
    run = runs.start_agent(t["dev"], t["project"].slug, "", mode="interactive")
    page = browser_page
    page.goto(live_server + "/login")
    page.fill("input[name=username]", "dev1")
    page.fill("input[name=password]", PASSWORD)
    page.click("button[type=submit]")
    cookie = "; ".join(f"{c['name']}={c['value']}" for c in page.context.cookies())
    url = live_server.replace("http://", "ws://") + f"/p/{t['project'].slug}/runs/{run.id}/ws"
    ws = simple_websocket.Client.connect(url, headers={"Cookie": cookie, "Origin": "http://127.0.0.1"})
    assert ws.receive(timeout=5)
    users.update(t["admin"], t["dev"].id, is_active=False)
    deadline = time.time() + 12
    with pytest.raises(simple_websocket.ConnectionClosed):
        while time.time() < deadline:
            ws.receive(timeout=1)
    runs.stop(t["owner"], run.id)


def test_git_ignores_repo_fsmonitor(team_project, git_project, tmp_path):
    from sagent.core import gitutil

    marker = tmp_path / "pwned"
    subprocess.run(["git", "-C", str(git_project), "config", "core.fsmonitor", f"touch {marker}; false"], check=True)
    (git_project / "app.py").write_text("print('changed')\n")
    gitutil.git(str(git_project), "status", "--porcelain")
    gitutil.git(str(git_project), "diff", "HEAD")
    assert not marker.exists()
