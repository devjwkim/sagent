from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from sagent.core import harness, loopdef, projects, rbac, scanner
from sagent.core.errors import SagentError, ValidationError
from sagent.web.security import project_perm

bp = Blueprint("harness", __name__)


def _scan(refresh: bool = False) -> dict:
    result = None if refresh else projects.last_scan(g.project)
    if result is None:
        result = scanner.scan(g.project.path)
        projects.save_scan(g.user, g.project, result)
    return result


@bp.get("/p/<slug>/bootstrap")
@project_perm("harness.edit")
def bootstrap_form(slug):
    return render_template(
        "projects/bootstrap.html",
        scan=_scan(),
        templates=loopdef.TEMPLATES,
        agents=projects.AGENTS,
        browsers=harness.BROWSERS,
        existing={k: v is not None for k, v in harness.read_all(g.project).items()},
    )


@bp.post("/p/<slug>/scan")
@project_perm("harness.edit")
def rescan(slug):
    _scan(refresh=True)
    flash("프로젝트를 다시 분석했습니다.", "ok")
    return redirect(url_for("harness.bootstrap_form", slug=slug))


@bp.post("/p/<slug>/bootstrap")
@project_perm("harness.edit")
def bootstrap_apply(slug):
    f = request.form
    answers = {
        "coding_agent": f.get("coding_agent"),
        "review_agent": f.get("review_agent"),
        "loop": f.get("loop", "standard"),
        "e2e": f.get("e2e") == "1",
        "browsers": f.getlist("browsers"),
        "visual_regression": f.get("visual_regression", "off"),
        "commands": {k: f.get(f"cmd_{k}", "").strip() for k in ("unit", "lint", "typecheck", "e2e")},
    }
    try:
        result = harness.bootstrap(g.user, slug, _scan(), answers, overwrite=f.get("overwrite") == "1")
    except SagentError as exc:
        flash(str(exc), "error")
        return redirect(url_for("harness.bootstrap_form", slug=slug))
    g.project, g.project_role = projects.get(g.user, slug)
    return render_template("projects/bootstrap_done.html", result=result)


@bp.get("/p/<slug>/harness")
@project_perm("project.view")
def harness_view(slug):
    files = harness.read_all(g.project)
    editable = {
        name: rbac.role_allows(g.project_role, perm) for name, perm in harness.FILE_PERM.items()
    }
    return render_template(
        "projects/harness.html",
        files=files,
        editable=editable,
        selected=request.args.get("file", harness.FILES[1]),
        can_bootstrap=rbac.role_allows(g.project_role, "harness.edit"),
    )


@bp.post("/p/<slug>/harness/<name>")
@project_perm("project.view")
def harness_save(slug, name):
    try:
        harness.save_file(g.user, slug, name, request.form.get("content", "").replace("\r\n", "\n"))
        flash(f"{name} 을(를) 저장했습니다.", "ok")
    except ValidationError as exc:
        flash(str(exc), "error")
    return redirect(url_for("harness.harness_view", slug=slug, file=name))
