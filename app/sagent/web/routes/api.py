"""JSON API v1 (bearer token auth). Same permissions as the web UI."""
from __future__ import annotations

from dataclasses import asdict

from flask import Blueprint, abort, g, jsonify, request

from sagent.core import loops, projects, reviews, runs, tests, usage
from sagent.core.errors import Conflict, Forbidden, NotFound, SagentError, ValidationError

bp = Blueprint("api", __name__, url_prefix="/api/v1")

RUN_FIELDS = ("id", "kind", "mode", "provider", "role", "title", "status", "model", "agent_session_id",
              "loop_run_id", "node_key", "exit_code", "summary", "error", "input_tokens", "output_tokens",
              "cache_read_tokens", "cache_write_tokens", "cost_usd", "duration_ms", "created_at", "started_at",
              "finished_at")


@bp.before_request
def _auth():
    if g.get("user") is None:
        return jsonify(error="invalid or missing bearer token"), 401
    if request.method not in ("GET", "HEAD") and g.get("api_scope") != "write":
        return jsonify(error="token scope 'write' required"), 403
    return None


@bp.errorhandler(NotFound)
def _nf(exc):
    return jsonify(error=str(exc) or "not found"), 404


@bp.errorhandler(Forbidden)
def _fb(exc):
    return jsonify(error=str(exc) or "forbidden"), 403


@bp.errorhandler(ValidationError)
@bp.errorhandler(Conflict)
@bp.errorhandler(SagentError)
def _bad(exc):
    return jsonify(error=str(exc)), 400


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400)
    return data


def _run(r) -> dict:
    d = asdict(r)
    return {k: d[k] for k in RUN_FIELDS}


@bp.get("/me")
def me():
    u = g.user
    return jsonify(username=u.username, display_name=u.display_name, role=u.role, scope=g.api_scope)


@bp.get("/projects")
def project_list():
    return jsonify(projects=[{"slug": p.slug, "name": p.name, "lifecycle": p.lifecycle,
                              "primary_agent": p.primary_agent, "role": role}
                             for p, role in projects.list_for(g.user)])


@bp.get("/projects/<slug>/runs")
def run_list(slug):
    limit = request.args.get("limit", 20, type=int)
    return jsonify(runs=[_run(r) for r in runs.list_for_project(g.user, slug, limit=limit)])


@bp.post("/projects/<slug>/runs")
def run_start(slug):
    b = _body()
    run = runs.start_agent(g.user, slug, str(b.get("prompt", "")), provider=b.get("provider") or None,
                           title=str(b.get("title", ""))[:200])
    return jsonify(run=_run(run)), 201


@bp.get("/runs/<int:run_id>")
def run_get(run_id):
    run, _, _ = runs.get(g.user, run_id)
    return jsonify(run=_run(run))


@bp.post("/runs/<int:run_id>/stop")
def run_stop(run_id):
    return jsonify(run=_run(runs.stop(g.user, run_id)))


@bp.post("/projects/<slug>/loops")
def loop_start(slug):
    b = _body()
    lr = loops.start(g.user, slug, str(b.get("loop", "")), str(b.get("task", "")), provider=b.get("provider") or None)
    return jsonify(loop_run=_loop(lr)), 201


def _loop(lr) -> dict:
    return {"id": lr.id, "loop": lr.loop_name, "status": lr.status, "current_node": lr.current_node,
            "iteration": lr.iteration, "error": lr.error, "created_at": lr.created_at,
            "finished_at": lr.finished_at,
            "nodes": [{"node": r["node_key"], "status": r["status"], "outcome": r["outcome"],
                       "run_id": r["run_id"], "visit": r["visit"], "attempt": r["attempt"]}
                      for r in loops.node_rows(lr.id)]}


@bp.get("/loops/<int:loop_run_id>")
def loop_get(loop_run_id):
    lr, _, _ = loops.get(g.user, loop_run_id)
    return jsonify(loop_run=_loop(lr))


@bp.post("/loops/<int:loop_run_id>/stop")
def loop_stop(loop_run_id):
    return jsonify(loop_run=_loop(loops.stop(g.user, loop_run_id)))


@bp.post("/projects/<slug>/tests")
def test_start(slug):
    b = _body()
    run = tests.start_suite(g.user, slug, str(b.get("suite", "unit")))
    return jsonify(run=_run(run)), 201


@bp.post("/projects/<slug>/reviews")
def review_start(slug):
    b = _body()
    run = reviews.start(g.user, slug, base_ref=str(b.get("base_ref") or "HEAD"), provider=b.get("provider") or None)
    return jsonify(run=_run(run)), 201


@bp.get("/reviews/by-run/<int:run_id>")
def review_by_run(run_id):
    runs.get(g.user, run_id)
    from sagent import db

    row = db.query_one("SELECT * FROM review_runs WHERE run_id = ?", (run_id,))
    if not row:
        raise NotFound("not found")
    rr, _, _ = reviews.get(g.user, row["id"])
    return jsonify(review={**asdict(rr), "issues": [dict(i) for i in reviews.issues(rr.id)]})


@bp.get("/usage")
def usage_get():
    days = usage.RANGES.get(request.args.get("days", "7"), 7)
    slug = request.args.get("project")
    if slug:
        project, _ = projects.get(g.user, slug, "usage.view")
        scope = {"project_id": project.id}
    else:
        scope = {"user_id": g.user.id, "project_ids": usage.visible_project_ids(g.user)}
    return jsonify(days=days, totals=usage.totals(days=days, **scope),
                   by_model=usage.breakdown("model", days=days, **scope))

