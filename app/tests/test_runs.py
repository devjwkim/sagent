import json
import time

import pytest

from sagent import db
from sagent.agents.codex import CodexAdapter
from sagent.core import runs
from sagent.core.errors import Forbidden, NotFound, ValidationError
from sagent.runtime import tmux


def _events(run_id):
    return [r["type"] for r in db.query("SELECT type FROM events WHERE run_id = ? ORDER BY id", (run_id,))]


def test_claude_auto_run_success(team_project, monkeypatch):
    monkeypatch.setenv("SAGENT_SECRET_KEY", "super-secret-value")
    t = team_project
    run = runs.start_agent(t["dev"], t["project"].slug, "add a login page")
    assert run.status == "RUNNING" and run.tmux_session.startswith("sagent-team-")
    run = runs.wait(run.id, timeout=30, poll=0.3)
    assert run.status == "SUCCESS", run.error
    assert (run.input_tokens, run.output_tokens, run.cache_read_tokens, run.cache_write_tokens) == (100, 20, 5, 1)
    assert run.cost_usd == pytest.approx(0.0123)
    assert run.model == "claude-test" and run.summary == "all done"
    types = _events(run.id)
    for t_ in ("run.started", "agent.start", "agent.message", "agent.shell.start", "agent.file.write",
               "agent.tool.end", "agent.usage", "agent.stop", "run.completed"):
        assert t_ in types, t_
    _, screen = runs.terminal(t["dev"], run.id)
    assert "working on: add a login page" in screen and "▶ Bash ls -la" in screen
    # sagent's own secret never reaches the agent process
    assert "super-secret-value" not in screen
    assert not tmux.exists(run.tmux_session)


def test_claude_failure_marks_failed(team_project):
    t = team_project
    run = runs.wait(runs.start_agent(t["dev"], t["project"].slug, "please FAIL").id, timeout=30, poll=0.3)
    assert run.status == "FAILED" and "it broke" in run.error


def test_codex_run(team_project):
    t = team_project
    run = runs.start_agent(t["dev"], t["project"].slug, "fix tests", provider="codex")
    run = runs.wait(run.id, timeout=30, poll=0.3)
    assert run.status == "SUCCESS", run.error
    assert run.agent_session_id == "thread-abc"
    assert (run.input_tokens, run.output_tokens, run.cache_read_tokens) == (300, 40, 50)
    types = _events(run.id)
    assert "agent.shell.start" in types and "agent.shell.end" in types and "agent.file.write" in types


def test_stop_run(team_project):
    t = team_project
    run = runs.start_agent(t["dev"], t["project"].slug, "SLEEP please")
    time.sleep(1)
    with pytest.raises(Forbidden):
        runs.stop(t["dev2"], run.id)  # other developer: own runs only
    runs.stop(t["dev"], run.id)
    run = runs.wait(run.id, timeout=5)
    assert run.status == "CANCELLED"
    assert not tmux.exists(run.tmux_session)
    assert "run.cancelled" in _events(run.id)


def test_maintainer_or_owner_controls_any_run(team_project):
    t = team_project
    run = runs.start_agent(t["dev"], t["project"].slug, "SLEEP more")
    runs.stop(t["owner"], run.id)
    assert runs.wait(run.id, timeout=5).status == "CANCELLED"


def test_command_runs(team_project):
    t = team_project
    semi = runs.wait(runs.start_command(t["dev"], t["project"].slug, "echo one; echo two;").id, timeout=20, poll=0.3)
    assert semi.status == "SUCCESS" and "two" in runs.terminal(t["dev"], semi.id)[1]
    ok = runs.wait(runs.start_command(t["dev"], t["project"].slug, "echo hello-world").id, timeout=20, poll=0.3)
    assert ok.status == "SUCCESS" and ok.exit_code == 0
    assert "hello-world" in runs.terminal(t["dev"], ok.id)[1]
    bad = runs.wait(runs.start_command(t["dev"], t["project"].slug, "exit 3").id, timeout=20, poll=0.3)
    assert bad.status == "FAILED" and bad.exit_code == 3


def test_interactive_run_with_input(team_project):
    t = team_project
    run = runs.start_agent(t["dev"], t["project"].slug, "", mode="interactive")
    deadline = time.time() + 10
    while "ready" not in runs.terminal(t["dev"], run.id)[1] and time.time() < deadline:
        time.sleep(0.2)
    with pytest.raises(Forbidden):
        runs.send_input(t["dev2"], run.id, "hello")
    runs.send_input(t["dev"], run.id, "hello there")
    deadline = time.time() + 10
    while "you said: hello there" not in runs.terminal(t["dev"], run.id)[1] and time.time() < deadline:
        time.sleep(0.2)
    assert "you said: hello there" in runs.terminal(t["dev"], run.id)[1]
    runs.send_input(t["dev"], run.id, "a;b ; c;")  # ';' must not be parsed by tmux
    deadline = time.time() + 10
    while "you said: a;b ; c;" not in runs.terminal(t["dev"], run.id)[1] and time.time() < deadline:
        time.sleep(0.2)
    assert "you said: a;b ; c;" in runs.terminal(t["dev"], run.id)[1]
    with pytest.raises(ValidationError):
        runs.send_key(t["dev"], run.id, "C-z")  # not whitelisted
    runs.send_input(t["dev"], run.id, "exit")
    run = runs.wait(run.id, timeout=15, poll=0.3)
    assert run.status == "SUCCESS"
    assert "terminal.input" in _events(run.id)


def test_permissions(team_project, make_user):
    t = team_project
    with pytest.raises(Forbidden):
        runs.start_agent(t["viewer"], t["project"].slug, "x")
    outsider = make_user("eve")
    with pytest.raises(NotFound):
        runs.start_agent(outsider, t["project"].slug, "x")
    run = runs.wait(runs.start_agent(t["dev"], t["project"].slug, "hi").id, timeout=30, poll=0.3)
    with pytest.raises(NotFound):
        runs.get(outsider, run.id)
    with pytest.raises(Forbidden):
        runs.terminal(t["viewer"], run.id)
    runs.get(t["viewer"], run.id)  # viewer may see the run itself


def test_invalid_resume_id_rejected(team_project):
    t = team_project
    with pytest.raises(ValidationError):
        runs.start_agent(t["dev"], t["project"].slug, "x", resume_session="abc; rm -rf /")


def test_session_name_validation():
    with pytest.raises(tmux.TmuxError):
        tmux.exists("bad name; rm")
    assert tmux.session_name("my-proj", 12) == "sagent-my-proj-12"
    assert len(tmux.session_name("x" * 80, 123456)) <= 64


def test_codex_adapter_command():
    from pathlib import Path

    from sagent.agents.base import RunSpec

    a = CodexAdapter()
    assert a.build_command(RunSpec(cwd=Path("/tmp"))) == [
        "codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write", "-"]
    assert a.build_command(RunSpec(cwd=Path("/tmp"), resume_session="t1"))[:4] == ["codex", "exec", "resume", "t1"]


def test_run_pages_over_http(team_project, new_browser):
    t = team_project
    run = runs.wait(runs.start_agent(t["dev"], t["project"].slug, "hello web").id, timeout=30, poll=0.3)
    slug = t["project"].slug
    d = new_browser()
    d.login("dev1")
    assert d.get(f"/p/{slug}/runs").status_code == 200
    assert d.get(f"/p/{slug}/runs/{run.id}").status_code == 200
    term = d.get(f"/p/{slug}/runs/{run.id}/terminal")
    assert term.status_code == 286 and "hello web" in term.get_data(as_text=True)
    ev = d.get(f"/p/{slug}/runs/{run.id}/events")
    assert "agent.message" in ev.get_data(as_text=True)

    v = new_browser()
    v.login("vic")
    assert v.get(f"/p/{slug}/runs/{run.id}").status_code == 200
    assert v.get(f"/p/{slug}/runs/{run.id}/terminal").status_code == 403
    evv = v.get(f"/p/{slug}/runs/{run.id}/events").get_data(as_text=True)
    assert "run.completed" in evv and "agent.message" not in evv  # viewers only see lifecycle
    assert v.post(f"/p/{slug}/runs", {"prompt": "x"}).status_code == 403
    # run id from another project is not reachable through this project's URL
    assert d.get(f"/p/{slug}/runs/99999").status_code == 404


def test_start_run_over_http(team_project, new_browser):
    t = team_project
    d = new_browser()
    d.login("dev1")
    resp = d.post(f"/p/{t['project'].slug}/runs", {"prompt": "via web", "provider": "claude", "mode": "auto"})
    assert resp.status_code == 302
    run_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    assert runs.wait(run_id, timeout=30, poll=0.3).status == "SUCCESS"


def test_terminal_log_download(team_project, new_browser):
    t = team_project
    run = runs.wait(runs.start_command(t["dev"], t["project"].slug, "echo recorded-output").id, timeout=20, poll=0.3)
    slug = t["project"].slug
    d = new_browser()
    d.login("dev1")
    resp = d.get(f"/p/{slug}/runs/{run.id}/terminal.log")
    assert resp.status_code == 200 and b"recorded-output" in resp.data
    assert resp.headers["Content-Security-Policy"].startswith("sandbox")
    v = new_browser()
    v.login("vic")
    assert v.get(f"/p/{slug}/runs/{run.id}/terminal.log").status_code == 403
