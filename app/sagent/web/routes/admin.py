from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from sagent.core import audit, doctor, settings, users
from sagent.core.errors import SagentError
from sagent.web.security import admin_required

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/users", methods=["GET", "POST"])
@admin_required
def user_list():
    if request.method == "POST":
        f = request.form
        try:
            u = users.create(
                g.user,
                f.get("username", ""),
                f.get("password", ""),
                role=f.get("role", "member"),
                display_name=f.get("display_name", ""),
            )
            flash(f"사용자 {u.username} 을(를) 만들었습니다. 첫 로그인 때 비밀번호를 바꾸게 됩니다.", "ok")
        except SagentError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin.user_list"))
    return render_template("admin/users.html", users=users.list_all(), roles=users.ROLES)


@bp.post("/users/<int:user_id>")
@admin_required
def user_update(user_id):
    f = request.form
    try:
        action = f.get("action")
        if action == "reset_password":
            users.reset_password(g.user, user_id, f.get("password", ""))
            flash("비밀번호를 초기화했습니다. 다음 로그인 때 변경을 요구합니다.", "ok")
        elif action == "toggle_active":
            target = users.get(user_id)
            users.update(g.user, user_id, is_active=not target.is_active)
            flash("상태를 변경했습니다.", "ok")
        elif action == "role":
            users.update(g.user, user_id, role=f.get("role", ""))
            flash("역할을 변경했습니다.", "ok")
    except SagentError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.user_list"))


@bp.get("/audit")
@admin_required
def audit_log():
    page = max(request.args.get("page", 1, type=int), 1)
    rows = audit.list_entries(
        limit=100,
        offset=(page - 1) * 100,
        action=request.args.get("action", ""),
        username=request.args.get("user", ""),
    )
    return render_template("admin/audit.html", rows=rows, page=page)


@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def server_settings():
    if request.method == "POST":
        f = request.form
        if f.get("action") == "clear_lockouts":
            n = users.clear_lockouts(g.user, f.get("ip") or None)
            flash(f"로그인 잠금 기록 {n}건을 지웠습니다.", "ok")
        else:
            roots = "\n".join(
                line.strip() for line in f.get("allowed_roots", "").splitlines() if line.strip()
            )
            settings.put("projects.allowed_roots", roots, g.user)
            settings.put(
                "projects.member_can_create", "1" if f.get("member_can_create") else "0", g.user
            )
            try:
                settings.put("runs.command_prefix", settings.validate_command_prefix(f.get("command_prefix", "")), g.user)
            except SagentError as exc:
                flash(str(exc), "error")
                return redirect(url_for("admin.server_settings"))
            for key in ("auth.lockout_threshold", "auth.lockout_window_min", "runs.log_retention_days"):
                val = f.get(key, "").strip()
                if val.isdigit():
                    settings.put(key, val, g.user)
            flash("설정을 저장했습니다.", "ok")
        return redirect(url_for("admin.server_settings"))
    return render_template(
        "admin/settings.html",
        allowed_roots="\n".join(settings.get_lines("projects.allowed_roots")),
        member_can_create=settings.get_bool("projects.member_can_create"),
        lockout_threshold=settings.get_int("auth.lockout_threshold"),
        lockout_window=settings.get_int("auth.lockout_window_min"),
        retention_days=settings.get_int("runs.log_retention_days"),
        command_prefix=settings.get("runs.command_prefix"),
        checks=doctor.run_checks(),
    )
