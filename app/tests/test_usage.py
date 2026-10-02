import json
import time

import pytest
import yaml

from sagent import db
from sagent.core import harness, loops, prompts, runs, scanner, settings, telemetry, usage
from sagent.core.errors import Forbidden, ValidationError


def _run(t, who="dev", prompt="hello", **kw):
    return runs.wait(runs.start_agent(t[who], t["project"].slug, prompt, **kw).id, timeout=30, poll=0.3)


def test_usage_record_per_run(team_project):
    t = team_project
    run = _run(t)
    rec = db.query_one("SELECT * FROM usage_records WHERE run_id = ?", (run.id,))
    assert rec["user_id"] == t["dev"].id and rec["project_id"] == t["project"].id
    assert (rec["input_tokens"], rec["output_tokens"]) == (100, 20)
    assert rec["cost_usd"] == pytest.approx(0.0123) and rec["cost_source"] == "agent"
    assert rec["model"] == "claude-test" and rec["status"] == "SUCCESS"


def test_estimate_cost_longest_prefix(app):
    usage.seed_prices()
    c55 = usage.estimate_cost("claude-opus-5-5", 1_000_000, 0, 0, 0)
    c5 = usage.estimate_cost("claude-opus-5", 1_000_000, 0, 0, 0)
    assert c55 == pytest.approx(4.0) and c5 == pytest.approx(5.0)
    assert usage.estimate_cost("claude-haiku-4-5-20251001", 0, 1_000_000, 1_000_000, 0) == pytest.approx(5.1)
    assert usage.estimate_cost("unknown-model", 1, 1, 1, 1) is None


def test_interactive_transcript_ingest(team_project, tmp_path, monkeypatch):
    t = team_project
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    run = runs.start_agent(t["dev"], t["project"].slug, "", mode="interactive")
    sid = run.agent_session_id
    tdir = tmp_path / "claude" / "projects" / "-some-project"
    tdir.mkdir(parents=True)
    lines = [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"id": "m1", "model": "claude-haiku-4-5-20251001",
                                           "usage": {"input_tokens": 10, "output_tokens": 1}}},
        {"type": "assistant", "message": {"id": "m1", "model": "claude-haiku-4-5-20251001",
                                           "usage": {"input_tokens": 10, "output_tokens": 50,
                                                     "cache_read_input_tokens": 1000}}},
        {"type": "assistant", "message": {"id": "m2", "model": "claude-haiku-4-5-20251001",
                                           "usage": {"input_tokens": 5, "output_tokens": 5,
                                                     "cache_creation_input_tokens": 200}}},
    ]
    (tdir / f"{sid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    time.sleep(1)
    runs.send_input(t["dev"], run.id, "exit")
    run = runs.wait(run.id, timeout=15, poll=0.3)
    rec = db.query_one("SELECT * FROM usage_records WHERE run_id = ?", (run.id,))
    assert (rec["input_tokens"], rec["output_tokens"], rec["cache_read_tokens"], rec["cache_write_tokens"]) == \
        (15, 55, 1000, 200)
    assert rec["cost_source"] == "estimate" and rec["model"].startswith("claude-haiku-4-5")
    expected = (15 * 1 + 55 * 5 + 1000 * 0.1 + 200 * 1.25) / 1e6
    assert rec["cost_usd"] == pytest.approx(expected)


def test_aggregates_and_dashboards(team_project, new_browser, make_user):
    t = team_project
    _run(t, "dev")
    _run(t, "owner")
    tot = usage.totals(project_id=t["project"].id, days=7)
    assert tot["runs"] == 2 and tot["tokens"] == 2 * 126 and tot["success_rate"] == 1.0
    users_bd = {r["label"]: r["tokens"] for r in usage.breakdown("user", project_id=t["project"].id)}
    assert users_bd == {"dev1": 126, "olivia": 126}
    days = usage.by_day(days=7, project_id=t["project"].id)
    assert len(days) == 7 and days[-1]["claude"] == 252

    slug = t["project"].slug
    d = new_browser()
    d.login("dev1")
    mine = d.get("/usage").get_data(as_text=True)
    assert "<svg" in mine and "126" in mine  # only own usage
    assert "252" not in mine
    assert d.get(f"/p/{slug}/usage?days=30").status_code == 200
    assert d.get("/admin/usage").status_code == 403
    v = new_browser()
    v.login("vic")
    assert v.get(f"/p/{slug}/usage").status_code == 200  # viewer has usage.view
    make_user("eve")
    e = new_browser()
    e.login("eve")
    assert e.get(f"/p/{slug}/usage").status_code == 404
    assert "126" not in e.get("/usage").get_data(as_text=True)
    a = new_browser()
    a.login("admin1")
    assert "dev1" in a.get("/admin/usage").get_data(as_text=True)


def test_prices_admin(team_project, new_browser):
    t = team_project
    with pytest.raises(Forbidden):
        usage.set_price(t["dev"], "gpt-5", "codex", {"input": 1})
    with pytest.raises(ValidationError):
        usage.set_price(t["admin"], "bad prefix!", "codex", {"input": 1})
    with pytest.raises(ValidationError):
        usage.set_price(t["admin"], "gpt-5", "codex", {"input": "-3"})
    usage.set_price(t["admin"], "gpt-5", "codex", {"input": "1.25", "output": "10"})
    assert usage.estimate_cost("gpt-5-codex", 1_000_000, 1_000_000, 0, 0) == pytest.approx(11.25)
    a = new_browser()
    a.login("admin1")
    assert "gpt-5" in a.get("/admin/prices").get_data(as_text=True)


def test_prompt_templates_versioned_and_used_by_loop(team_project):
    t = team_project
    slug = t["project"].slug
    with pytest.raises(Forbidden):
        prompts.save(t["dev"], slug, "impl", "x {task}")
    assert prompts.save(t["owner"], slug, "impl", "V1 MAKEFILE=v1.txt {task}")["version"] == 1
    assert prompts.save(t["owner"], slug, "impl", "V2 MAKEFILE=v2.txt {task}")["version"] == 2
    with pytest.raises(ValidationError):
        prompts.save(t["owner"], slug, "impl", "V2 MAKEFILE=v2.txt {task}")  # unchanged
    harness.bootstrap(t["owner"], slug, scanner.scan(t["project"].path), {}, overwrite=True)
    path = t["project"].path + "/.sagent/loops.yaml"
    data = yaml.safe_load(open(path))
    data["loops"]["tpl"] = {"start": "implement", "nodes": {
        "implement": {"type": "agent", "prompt_ref": "impl@1"}, "done": {"type": "end"}},
        "edges": [{"from": "implement", "to": "done"}]}
    data["loops"]["tpl_latest"] = {"start": "implement", "nodes": {
        "implement": {"type": "agent", "prompt_ref": "impl"}, "done": {"type": "end"}},
        "edges": [{"from": "implement", "to": "done"}]}
    open(path, "w").write(yaml.safe_dump(data))
    lr1 = loops.wait(loops.start(t["dev"], slug, "tpl", "task one").id, timeout=60, poll=0.3)
    lr2 = loops.wait(loops.start(t["dev"], slug, "tpl_latest", "task two").id, timeout=60, poll=0.3)
    assert lr1.status == lr2.status == "SUCCESS"
    r1 = runs._load(loops.node_rows(lr1.id)[0]["run_id"])
    r2 = runs._load(loops.node_rows(lr2.id)[0]["run_id"])
    assert (r1.prompt_template, r1.prompt_version) == ("impl", 1) and r1.prompt.startswith("V1")
    assert (r2.prompt_template, r2.prompt_version) == ("impl", 2)
    stats = {s["version"]: s for s in prompts.stats(t["dev"], slug, "impl")}
    assert stats[1]["runs"] == 1 and stats[2]["runs"] == 1


def test_bad_prompt_ref_rejected():
    from sagent.core import loopdef

    loop = loopdef.template("quick")
    loop["nodes"]["implement"]["prompt_ref"] = "Bad Ref!"
    assert any("prompt_ref" in e for e in loopdef.validate_loop("x", loop))


def test_otel_span_export(team_project, monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    mem = InMemorySpanExporter()
    tr = telemetry.tracer(exporter=mem)
    monkeypatch.setattr(telemetry, "tracer", lambda exporter=None: tr)
    run = _run(team_project)
    spans = mem.get_finished_spans()
    span = [s for s in spans if s.attributes.get("run.id") == run.id][0]
    a = span.attributes
    assert span.name == "sagent.run.agent"
    assert a["user.id"] == "dev1" and a["project.id"] == team_project["project"].slug
    assert a["agent.provider"] == "claude" and a["input_tokens"] == 100 and a["status"] == "SUCCESS"


def test_otel_env_for_claude(team_project):
    t = team_project
    slug = t["project"].slug
    harness.bootstrap(t["owner"], slug, scanner.scan(t["project"].path), {}, overwrite=True)
    path = t["project"].path + "/.sagent/harness.yaml"
    data = yaml.safe_load(open(path))
    data["telemetry"]["otel"] = True
    open(path, "w").write(yaml.safe_dump(data))
    settings.put("otel.endpoint", "http://127.0.0.1:4318")
    settings.put("otel.headers", "authorization=Bearer abc")
    assert db.scalar("SELECT value FROM settings WHERE key='otel.headers'") != "authorization=Bearer abc"
    run = runs.start_agent(t["dev"], slug, "hi")
    env = json.loads((runs.run_dir(run.id) / "spec.json").read_text())["env"]
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in env  # admin opt-in required
    runs.wait(run.id, timeout=30, poll=0.3)
    settings.put("otel.agent_env", "1")
    run = runs.start_agent(t["dev"], slug, "hi")
    env = json.loads((runs.run_dir(run.id) / "spec.json").read_text())["env"]
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1" and env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4318"
    assert "sagent.user=dev1" in env["OTEL_RESOURCE_ATTRIBUTES"]
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env  # the org credential needs its own opt-in
    runs.wait(run.id, timeout=30, poll=0.3)
    settings.put("otel.agent_headers", "1")
    run = runs.start_agent(t["dev"], slug, "hi")
    env = json.loads((runs.run_dir(run.id) / "spec.json").read_text())["env"]
    assert env["OTEL_EXPORTER_OTLP_HEADERS"] == "authorization=Bearer abc"
    settings.put("otel.endpoint", "")
    runs.wait(run.id, timeout=30, poll=0.3)


def test_log_retention(team_project):
    t = team_project
    run = _run(t)
    d = runs.run_dir(run.id)
    assert d.exists()
    db.execute("UPDATE runs SET finished_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (run.id,))
    assert runs.purge_old_logs(30) == 1
    assert not d.exists()
    assert db.scalar("SELECT COUNT(*) FROM usage_records WHERE run_id = ?", (run.id,)) == 1


def test_prompt_pages(team_project, new_browser):
    t = team_project
    slug = t["project"].slug
    o = new_browser()
    o.login("olivia")
    resp = o.post(f"/p/{slug}/prompts", {"name": "fixer", "body": "Fix: {task}", "note": "first"})
    assert resp.status_code == 302
    assert "Fix: {task}" in o.get(f"/p/{slug}/prompts/fixer").get_data(as_text=True)
    d = new_browser()
    d.login("dev1")
    assert d.get(f"/p/{slug}/prompts").status_code == 200
    assert d.post(f"/p/{slug}/prompts", {"name": "x", "body": "y"}).status_code == 403
