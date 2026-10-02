import json
import os

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for

from sagent import db
from sagent.core import harness, rbac, runs, tests, users
from sagent.core.errors import SagentError, ValidationError
from sagent.web.security import project_perm

bp = Blueprint("tests", __name__)

# Test artifacts (Playwright report, screenshots, arbitrary attachments) are
# untrusted HTML/JS from the project under test: serve them in a CSP sandbox
# (opaque origin) so they can never read sagent cookies or call its API.
ARTIFACT_CSP = (
    "sandbox allow-scripts allow-popups allow-downloads allow-forms; "
    "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
    "style-src 'self' 'unsafe-inline' data:; img-src 'self' data: blob:; font-src 'self' data:; "
    "connect-src 'self' data: blob:; frame-ancestors 'self'"
)


def _tr_in_project(test_run_id: int, perm: str = "project.view"):
    tr, project, _ = tests.get(g.user, test_run_id, perm)
    if project.id != g.project.id:
        abort(404)
    return tr


@bp.get("/p/<slug>/tests")
@project_perm("project.view")
def test_list(slug):
    cfg = harness.load(g.project)["tests.yaml"]
    available = []
    for suite in tests.SUITES:
        c = cfg.get(suite) or {}
        cmd = (c.get("command") or "").strip()
        if suite == "e2e" and not c.get("enabled", True):
            cmd = ""
        available.append((suite, cmd))
    return render_template(
        "tests/list.html",
        test_runs=tests.list_for_project(g.user, slug, limit=100),
        available=available,
        can_run=rbac.role_allows(g.project_role, "run.start"),
        names={u.id: u.username for u in users.list_all()},
    )


@bp.post("/p/<slug>/tests")
@project_perm("run.start")
def test_start(slug):
    suite = request.form.get("suite", "")
    try:
        run = tests.start_suite(g.user, slug, suite)
    except ValidationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("tests.test_list", slug=slug))
    tid = db.scalar("SELECT id FROM test_runs WHERE run_id = ?", (run.id,))
    return redirect(url_for("tests.test_detail", slug=slug, test_run_id=tid))


def _rel(path: str, base: str) -> str | None:
    real = os.path.realpath(path)
    base = os.path.realpath(base)
    return os.path.relpath(real, base) if real.startswith(base + os.sep) else None


@bp.get("/p/<slug>/tests/<int:test_run_id>")
@project_perm("project.view")
def test_detail(slug, test_run_id):
    tr = _tr_in_project(test_run_id)
    full = rbac.role_allows(g.project_role, "terminal.view")
    base = str(tests.output_dir(tr.id))
    rows = []
    for c in tests.cases(tr.id):
        atts = []
        for a in json.loads(c["attachments"] or "[]"):
            rel = _rel(a.get("path") or "", base)
            if rel:
                atts.append({**a, "rel": rel})
        rows.append({**dict(c), "steps": json.loads(c["steps"] or "[]"), "atts": atts})
    run = runs._load(tr.run_id) if tr.run_id else None
    return render_template(
        "tests/detail.html", tr=tr, cases=rows, run=run, full=full,
        can_analyze=rbac.role_allows(g.project_role, "run.start"),
        can_update_snapshots=rbac.role_allows(g.project_role, "harness.edit"),
    )


@bp.get("/p/<slug>/tests/<int:test_run_id>/live")
@project_perm("project.view")
def test_live(slug, test_run_id):
    tr = _tr_in_project(test_run_id)
    run = runs._load(tr.run_id) if tr.run_id else None
    body = render_template("tests/_status.html", tr=tr, run=run)
    return body, (200 if tr.status == "RUNNING" else 286)


def _artifact_response(path):
    resp = send_file(path, conditional=True)
    resp.headers["Content-Security-Policy"] = ARTIFACT_CSP
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


@bp.get("/p/<slug>/tests/<int:test_run_id>/report/")
@bp.get("/p/<slug>/tests/<int:test_run_id>/report/<path:rel>")
@project_perm("terminal.view")
def test_report(slug, test_run_id, rel="index.html"):
    _tr_in_project(test_run_id, "terminal.view")
    return _artifact_response(tests.safe_artifact(g.user, test_run_id, f"playwright/report/{rel}"))


@bp.get("/p/<slug>/tests/<int:test_run_id>/file/<path:rel>")
@project_perm("terminal.view")
def test_file(slug, test_run_id, rel):
    _tr_in_project(test_run_id, "terminal.view")
    return _artifact_response(tests.safe_artifact(g.user, test_run_id, rel))


@bp.post("/p/<slug>/tests/<int:test_run_id>/analyze")
@project_perm("run.start")
def test_analyze(slug, test_run_id):
    _tr_in_project(test_run_id)
    try:
        run = tests.analyze_failures(g.user, test_run_id)
    except SagentError as exc:
        flash(str(exc), "error")
        return redirect(url_for("tests.test_detail", slug=slug, test_run_id=test_run_id))
    return redirect(url_for("runs.run_detail", slug=slug, run_id=run.id))


@bp.post("/p/<slug>/tests/<int:test_run_id>/update-snapshots")
@project_perm("project.view")
def test_update_snapshots(slug, test_run_id):
    _tr_in_project(test_run_id)
    try:
        run = tests.update_snapshots(g.user, test_run_id)
    except ValidationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("tests.test_detail", slug=slug, test_run_id=test_run_id))
    tid = db.scalar("SELECT id FROM test_runs WHERE run_id = ?", (run.id,))
    return redirect(url_for("tests.test_detail", slug=slug, test_run_id=tid))


@bp.route("/p/<slug>/tests/wizard", methods=["GET", "POST"])
@project_perm("run.start")
def test_wizard(slug):
    from sagent.core import scenarios

    if request.method == "POST":
        f = request.form
        chosen = f.getlist("scenario") + [x for x in f.get("custom", "").splitlines() if x.strip()]
        try:
            run = scenarios.start(g.user, slug, pages=f.getlist("page"), scenarios=chosen,
                                  browsers=f.getlist("browsers"), mobile=f.get("mobile") == "1",
                                  visual=f.get("visual", "off"), base_url=f.get("base_url", "").strip())
        except ValidationError as exc:
            flash(str(exc), "error")
            return redirect(url_for("tests.test_wizard", slug=slug))
        return redirect(url_for("runs.run_detail", slug=slug, run_id=run.id))
    e2e = harness.load(g.project)["tests.yaml"].get("e2e") or {}
    return render_template(
        "tests/wizard.html", pages=scenarios.detect_pages(g.project.path),
        defaults=scenarios.DEFAULT_SCENARIOS, e2e=e2e, browsers=["chromium", "firefox", "webkit"],
        can_edit_harness=rbac.role_allows(g.project_role, "harness.edit"),
    )
