from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from sagent import agents
from sagent.core import loopdef, loops, rbac, runs, users
from sagent.core.errors import Forbidden, SagentError, ValidationError
from sagent.web.graph import layout
from sagent.web.security import project_perm

bp = Blueprint("loops", __name__)
HTMX_STOP_POLLING = 286


def _loop_in_project(loop_run_id: int):
    lr, project, _ = loops.get(g.user, loop_run_id)
    if project.id != g.project.id:
        abort(404)
    return lr


@bp.get("/p/<slug>/loops")
@project_perm("project.view")
def loop_list(slug):
    defs, default = loops.definitions(g.project)
    graphs = {}
    errors = {}
    for name, spec in defs.items():
        errs = loopdef.validate_loop(name, spec)
        if errs:
            errors[name] = errs
        else:
            graphs[name] = layout(spec)
    return render_template(
        "loops/list.html",
        defs=defs, default=default, graphs=graphs, errors=errors,
        loop_runs=loops.list_for_project(g.user, slug),
        names={u.id: u.username for u in users.list_all()},
        can_start=rbac.role_allows(g.project_role, "run.start"),
        can_edit=rbac.role_allows(g.project_role, "loop.edit"),
        providers=agents.names(),
    )


@bp.post("/p/<slug>/loops")
@project_perm("run.start")
def loop_start(slug):
    f = request.form
    try:
        lr = loops.start(g.user, slug, f.get("loop", ""), f.get("task", ""), provider=f.get("provider") or None)
    except SagentError as exc:
        flash(str(exc), "error")
        return redirect(url_for("loops.loop_list", slug=slug))
    return redirect(url_for("loops.loop_run", slug=slug, loop_run_id=lr.id))


def _ctx(lr):
    states = loops.node_states(lr)
    return {
        "lr": lr,
        "graph": layout(lr.spec, states),
        "states": states,
        "nodes": loops.node_rows(lr.id),
        "can_control": rbac.can_control_run(g.user, lr.project_id, lr.created_by, "run.control"),
    }


@bp.get("/p/<slug>/loops/runs/<int:loop_run_id>")
@project_perm("project.view")
def loop_run(slug, loop_run_id):
    lr = _loop_in_project(loop_run_id)
    return render_template(
        "loops/run.html", **_ctx(lr),
        names={u.id: u.username for u in users.list_all()},
        providers=agents.names(),
        child_runs={r.id: r for r in runs.list_for_project(g.user, slug, limit=500, loop_run_id=lr.id)},
    )


@bp.get("/p/<slug>/loops/runs/<int:loop_run_id>/live")
@project_perm("project.view")
def loop_run_live(slug, loop_run_id):
    lr = _loop_in_project(loop_run_id)
    body = render_template(
        "loops/_live.html", **_ctx(lr),
        child_runs={r.id: r for r in runs.list_for_project(g.user, slug, limit=500, loop_run_id=lr.id)},
    )
    return body, (200 if lr.is_active else HTMX_STOP_POLLING)


@bp.post("/p/<slug>/loops/runs/<int:loop_run_id>/control")
@project_perm("project.view")
def loop_control(slug, loop_run_id):
    _loop_in_project(loop_run_id)
    action = request.form.get("action")
    provider = request.form.get("provider") or None
    try:
        if action == "pause":
            loops.pause(g.user, loop_run_id)
        elif action == "resume":
            loops.resume(g.user, loop_run_id, provider)
        elif action == "stop":
            loops.stop(g.user, loop_run_id)
        elif action == "retry":
            loops.retry_node(g.user, loop_run_id, provider)
        elif action == "skip":
            loops.skip_node(g.user, loop_run_id)
        elif action == "rerun_from":
            loops.rerun_from(g.user, loop_run_id, request.form.get("node", ""), provider)
        elif action == "take_control":
            run = loops.take_control(g.user, loop_run_id, provider)
            return redirect(url_for("runs.run_detail", slug=slug, run_id=run.id, take=1))
        elif action == "return":
            loops.return_to_automation(g.user, loop_run_id, request.form.get("node") or None,
                                       request.form.get("note", ""))
        else:
            abort(400)
    except ValidationError as exc:
        flash(str(exc), "error")
    return redirect(url_for("loops.loop_run", slug=slug, loop_run_id=loop_run_id))


# --- visual editor ------------------------------------------------------------

@bp.get("/p/<slug>/loops/editor")
@bp.get("/p/<slug>/loops/editor/<name>")
@project_perm("loop.edit")
def loop_editor(slug, name=None):
    import copy

    defs, default = loops.definitions(g.project)
    if name is not None and name not in defs:
        abort(404)
    if name is None:
        spec = copy.deepcopy(loopdef.template("quick"))
        spec["description"] = ""
        new_name = "custom"
        i = 2
        while new_name in defs:
            new_name = f"custom{i}"
            i += 1
    else:
        spec, new_name = copy.deepcopy(defs[name]), name
    spec.setdefault("edges", [])
    positions = {n["key"]: {"x": n["x"], "y": n["y"]} for n in layout(spec)["nodes"]}
    from sagent.core import projects as projects_mod

    payload = {"name": new_name, "old_name": name, "loop": spec, "positions": positions,
               "lifecycles": list(projects_mod.LIFECYCLES)}
    return render_template("loops/editor.html", payload=payload, is_default=(name == default))


@bp.post("/p/<slug>/loops/editor")
@project_perm("loop.edit")
def loop_editor_save(slug):
    from flask import jsonify

    from sagent.core import loop_editor

    body = request.get_json(silent=True) or {}
    try:
        loop_editor.save(g.user, slug, str(body.get("name", "")), body.get("loop") or {},
                         old_name=body.get("old_name") or None, make_default=bool(body.get("make_default")))
    except ValidationError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, redirect=url_for("loops.loop_list", slug=slug))


@bp.post("/p/<slug>/loops/editor/<name>/delete")
@project_perm("loop.edit")
def loop_editor_delete(slug, name):
    from sagent.core import loop_editor

    try:
        loop_editor.delete(g.user, slug, name)
        flash(f"Loop {name} 을(를) 삭제했습니다.", "ok")
    except ValidationError as exc:
        flash(str(exc), "error")
    return redirect(url_for("loops.loop_list", slug=slug))


# --- template library -------------------------------------------------------------

@bp.route("/p/<slug>/loops/library", methods=["GET", "POST"])
@project_perm("project.view")
def loop_library(slug):
    from sagent.core import loop_library

    can_edit = rbac.role_allows(g.project_role, "loop.edit")
    if request.method == "POST":
        f = request.form
        try:
            action = f.get("action")
            if action == "add":
                name = loop_library.add_to_project(g.user, slug, f.get("template", ""), f.get("as_name", ""))
                flash(f"템플릿을 Loop '{name}' 으로 추가했습니다.", "ok")
            elif action == "share":
                loop_library.share(g.user, slug, f.get("loop", ""), f.get("template_name", ""), f.get("description", ""))
                flash("조직 템플릿으로 공유했습니다.", "ok")
            elif action == "delete":
                loop_library.delete(g.user, int(f.get("template_id", "0")))
                flash("템플릿을 삭제했습니다.", "ok")
        except Forbidden:
            raise
        except (SagentError, ValueError) as exc:
            flash(str(exc) or "잘못된 요청입니다.", "error")
        return redirect(url_for("loops.loop_library", slug=slug))
    defs, _ = loops.definitions(g.project)
    items = loop_library.list_all()
    return render_template("loops/library.html", items=items, graphs={t["name"]: layout(t["spec"]) for t in items},
                           project_loops=list(defs), can_edit=can_edit,
                           names={u.id: u.username for u in users.list_all()})
