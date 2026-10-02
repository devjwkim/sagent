from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from sagent.core import projects, rbac, users
from sagent.core.errors import Forbidden, SagentError
from sagent.web.security import login_required, project_perm

bp = Blueprint("home", __name__)


@bp.get("/")
@login_required
def dashboard():
    from sagent.core import overview

    items = projects.list_for(g.user)
    return render_template(
        "dashboard.html",
        items=items,
        summaries={p.id: overview.summary(p.id) for p, _ in items},
        can_create=projects.can_create(g.user),
    )


@bp.route("/projects/new", methods=["GET", "POST"])
@login_required
def project_new():
    if not projects.can_create(g.user):
        flash("프로젝트를 만들 권한이 없습니다. 관리자에게 허용 경로를 요청하세요.", "error")
        return redirect(url_for("home.dashboard"))
    form = request.form
    if request.method == "POST":
        try:
            p = projects.create(
                g.user,
                form.get("name", ""),
                form.get("path", ""),
                slug=form.get("slug", ""),
                description=form.get("description", ""),
                primary_agent=form.get("primary_agent", "claude"),
                create_dir=form.get("create_dir") == "1",
            )
        except SagentError as exc:
            flash(str(exc), "error")
        else:
            flash("프로젝트를 등록했습니다. 이제 Bootstrap 으로 하네스를 구성하세요.", "ok")
            return redirect(url_for("harness.bootstrap_form", slug=p.slug))
    from sagent.core import settings

    return render_template(
        "projects/new.html",
        form=form,
        agents=projects.AGENTS,
        allowed_roots=settings.get_lines("projects.allowed_roots"),
    )


@bp.get("/p/<slug>")
@project_perm("project.view")
def project_overview(slug):
    import json as _json

    from sagent.core import overview

    return render_template(
        "projects/overview.html",
        summary=overview.summary(g.project.id),
        activity=[dict(r) | {"data": _json.loads(r["data"] or "{}")} for r in overview.recent_activity(g.project.id)],
        scan=projects.last_scan(g.project),
        perms=rbac.permissions_for(g.project_role),
        lifecycles=projects.LIFECYCLES,
        agents=projects.AGENTS,
    )


@bp.post("/p/<slug>/settings")
@project_perm("project.settings")
def project_settings(slug):
    fields = {
        k: request.form[k]
        for k in ("name", "description", "lifecycle", "primary_agent")
        if k in request.form
    }
    try:
        projects.update(g.user, slug, **fields)
        flash("저장했습니다.", "ok")
    except SagentError as exc:
        flash(str(exc), "error")
    return redirect(url_for("home.project_overview", slug=slug))


@bp.post("/p/<slug>/archive")
@project_perm("project.archive")
def project_archive(slug):
    projects.set_archived(g.user, slug, request.form.get("archived") == "1")
    return redirect(url_for("home.project_overview", slug=slug))


@bp.route("/p/<slug>/members", methods=["GET", "POST"])
@project_perm("project.view")
def project_members(slug):
    can_manage = rbac.role_allows(g.project_role, "member.manage")
    if request.method == "POST":
        if not can_manage:
            raise Forbidden("멤버를 관리할 권한이 없습니다.")
        try:
            if request.form.get("action") == "remove":
                projects.remove_member(g.user, slug, int(request.form.get("user_id", "0")))
            else:
                projects.set_member(
                    g.user, slug, request.form.get("username", ""), request.form.get("role", "")
                )
            flash("멤버 정보를 저장했습니다.", "ok")
        except (SagentError, ValueError) as exc:
            flash(str(exc) or "잘못된 요청입니다.", "error")
        return redirect(url_for("home.project_members", slug=slug))
    # Only admins see the user directory; owners type the exact username so
    # project ownership does not become a way to enumerate every account.
    candidates = [u for u in users.list_all() if u.is_active] if can_manage and g.user.is_admin else []
    return render_template(
        "projects/members.html",
        members=projects.list_members(g.user, slug),
        can_manage=can_manage,
        roles=rbac.PROJECT_ROLES,
        candidates=candidates,
    )
