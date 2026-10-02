import json

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from sagent import agents
from sagent.core import harness, projects, rbac, runs, users
from sagent.core.errors import SagentError
from sagent.runtime import tmux
from sagent.web.security import project_perm

bp = Blueprint("runs", __name__)

# HTMX stops polling when it receives this status code.
HTMX_STOP_POLLING = 286
PUBLIC_EVENT_PREFIXES = ("run.", "loop.", "test.", "review.")


def _run_in_project(run_id: int, perm: str = "project.view"):
    run, project, role = runs.get(g.user, run_id, perm)
    if project.id != g.project.id:
        abort(404)
    return run


def _user_names() -> dict[int, str]:
    return {u.id: u.username for u in users.list_all()}


@bp.get("/p/<slug>/runs")
@project_perm("project.view")
def run_list(slug):
    cfg = harness.load(g.project)
    default_provider = ((cfg["harness.yaml"].get("agents") or {}).get("coding") or {}).get("provider")
    return render_template(
        "runs/list.html",
        runs=runs.list_for_project(g.user, slug, limit=100),
        names=_user_names(),
        can_start=rbac.role_allows(g.project_role, "run.start"),
        providers=[(a, agents.get(a)) for a in agents.names()],
        default_provider=default_provider or g.project.primary_agent,
    )


@bp.post("/p/<slug>/runs")
@project_perm("run.start")
def run_start(slug):
    f = request.form
    try:
        run = runs.start_agent(
            g.user, slug, f.get("prompt", ""),
            provider=f.get("provider") or None,
            mode=f.get("mode", "auto"),
            title=f.get("title", ""),
            resume_session=(f.get("resume_session") or "").strip() or None,
        )
    except SagentError as exc:
        flash(str(exc), "error")
        return redirect(url_for("runs.run_list", slug=slug))
    return redirect(url_for("runs.run_detail", slug=slug, run_id=run.id))


@bp.get("/p/<slug>/runs/<int:run_id>")
@project_perm("project.view")
def run_detail(slug, run_id):
    run = _run_in_project(run_id)
    g.csp_relax_styles = True  # xterm.js injects <style> elements
    can_view_terminal = rbac.role_allows(g.project_role, "terminal.view")
    return render_template(
        "runs/detail.html",
        run=run,
        names=_user_names(),
        can_view_terminal=can_view_terminal,
        can_control=rbac.can_control_run(g.user, run.project_id, run.created_by, "run.control"),
        can_input=rbac.can_control_run(g.user, run.project_id, run.created_by, "terminal.input"),
        keys=["Enter", "Escape", "Tab", "Up", "Down", "C-c", "BSpace"],
        prompt=runs.read_prompt(g.user, run_id) if can_view_terminal else "",
        response=runs.final_response(g.user, run_id) if can_view_terminal else "",
        attach=" ".join(tmux.attach_argv(run.tmux_session)) if run.tmux_session and run.is_active else "",
    )


@bp.get("/p/<slug>/runs/<int:run_id>/terminal")
@project_perm("terminal.view")
def run_terminal(slug, run_id):
    _run_in_project(run_id, "terminal.view")
    run, text = runs.terminal(g.user, run_id)
    body = render_template("runs/_terminal.html", text=text, run=run)
    return body, (200 if run.is_active else HTMX_STOP_POLLING)


@bp.get("/p/<slug>/runs/<int:run_id>/terminal.log")
@project_perm("terminal.view")
def run_terminal_log(slug, run_id):
    from flask import send_file

    run = _run_in_project(run_id, "terminal.view")
    path = runs.run_dir(run.id) / "terminal.log"
    if not path.is_file():
        abort(404)
    resp = send_file(path, mimetype="text/plain", as_attachment=True,
                     download_name=f"sagent-run-{run.id}-terminal.log")
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return resp


@bp.get("/p/<slug>/runs/<int:run_id>/events")
@project_perm("project.view")
def run_events(slug, run_id):
    run = _run_in_project(run_id)
    rows = runs.list_events(g.user, run_id, limit=1000)
    full = rbac.role_allows(g.project_role, "terminal.view")
    items = []
    for r in rows[-400:]:
        if not full and not r["type"].startswith(PUBLIC_EVENT_PREFIXES):
            continue
        items.append({"id": r["id"], "type": r["type"], "ts": r["ts"],
                      "data": json.loads(r["data"] or "{}") if full else {}})
    body = render_template("runs/_events.html", events=items, run=run)
    return body, (200 if run.is_active else HTMX_STOP_POLLING)


@bp.get("/p/<slug>/runs/<int:run_id>/summary")
@project_perm("project.view")
def run_summary(slug, run_id):
    run = _run_in_project(run_id)
    body = render_template("runs/_summary.html", run=run, names=_user_names())
    return body, (200 if run.is_active else HTMX_STOP_POLLING)


@bp.post("/p/<slug>/runs/<int:run_id>/stop")
@project_perm("project.view")
def run_stop(slug, run_id):
    _run_in_project(run_id)
    runs.stop(g.user, run_id)
    flash("Run 을 중지했습니다.", "ok")
    return redirect(url_for("runs.run_detail", slug=slug, run_id=run_id))


@bp.post("/p/<slug>/runs/<int:run_id>/input")
@project_perm("project.view")
def run_input(slug, run_id):
    _run_in_project(run_id)
    try:
        key = request.form.get("key")
        if key:
            runs.send_key(g.user, run_id, key)
        else:
            runs.send_input(g.user, run_id, request.form.get("text", ""),
                            enter=request.form.get("enter", "1") == "1")
    except SagentError as exc:
        if request.headers.get("HX-Request"):
            return f'<span class="flash-error small">{_escape(str(exc))}</span>', 400
        flash(str(exc), "error")
    if request.headers.get("HX-Request"):
        return '<span class="muted small">전송됨</span>'
    return redirect(url_for("runs.run_detail", slug=slug, run_id=run_id))


def _escape(s: str) -> str:
    from markupsafe import escape

    return str(escape(s))


# --- interactive terminal (WebSocket) -------------------------------------------

def _origin_ok() -> bool:
    """WebSocket upgrades are GET requests, so CSRF tokens do not apply:
    reject cross-site pages (Cross-Site WebSocket Hijacking) by Origin."""
    from urllib.parse import urlsplit

    origin = request.headers.get("Origin", "")
    if not origin:
        return False
    o = urlsplit(origin)
    return o.netloc == request.host and o.scheme in ("http", "https")


def register_ws(sock) -> None:
    @sock.route("/p/<slug>/runs/<int:run_id>/ws")
    def run_ws(ws, slug, run_id):
        from werkzeug.exceptions import HTTPException

        from sagent.core.errors import Forbidden, NotFound
        from sagent.runtime import pty_bridge

        if not _origin_ok():
            ws.close(reason=1008, message="bad origin")
            return
        # Authorise inside the handler and close cleanly (1008) instead of
        # letting NotFound/Forbidden escape after the WebSocket handshake.
        if g.get("user") is None:
            ws.close(reason=1008, message="login required")
            return
        try:
            g.project, g.project_role = projects.get(g.user, slug, "terminal.view")
            run = _run_in_project(run_id, "terminal.view")
        except (NotFound, Forbidden, HTTPException):
            ws.close(reason=1008, message="not found")
            return
        if not run.is_active or not run.tmux_session or not tmux.exists(run.tmux_session):
            ws.close(reason=1000, message="run is not active")
            return
        can_input = rbac.can_control_run(g.user, run.project_id, run.created_by, "terminal.input")
        user = g.user
        runs.emit(run, "terminal.attach", {"by": user.username, "read_only": not can_input}, user.id)
        typed = {"n": 0}

        def on_input(n):
            typed["n"] += n

        user_id, epoch = user.id, user.session_epoch

        def still_allowed():
            from sagent.core import users as users_mod
            from sagent.core.errors import NotFound as _NF

            try:
                u = users_mod.get(user_id)
            except _NF:
                return False, False
            if not u.is_active or u.session_epoch != epoch:
                return False, False
            return (rbac.can(u, run.project_id, "terminal.view"),
                    rbac.can_control_run(u, run.project_id, run.created_by, "terminal.input"))

        try:
            pty_bridge.bridge(ws, run.tmux_session, read_only=not can_input, on_input=on_input,
                              still_allowed=still_allowed)
        finally:
            runs.emit(run, "terminal.detach", {"by": user.username, "typed": typed["n"]}, user.id)
            if typed["n"]:
                from sagent.core import audit

                audit.record("run.terminal_input", user, "run", run.id, {"chars": typed["n"]})
