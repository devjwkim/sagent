from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from sagent.core import prompts, rbac, telemetry, usage
from sagent.core.errors import ValidationError
from sagent.web import charts
from sagent.web.security import admin_required, login_required, project_perm

bp = Blueprint("usage", __name__)


def _days() -> int:
    return usage.RANGES.get(request.args.get("days", "7"), 7)


def _dashboard(scope: dict, days: int, dims: list[str]) -> dict:
    daily = usage.by_day(days=days, **scope)
    breakdowns = {}
    for dim in dims:
        rows = usage.breakdown(dim, days=days, **scope)
        breakdowns[dim] = {"rows": rows, "chart": charts.hbars(rows)}
    return {
        "days": days,
        "totals": usage.totals(days=days, **scope),
        "daily": daily,
        "chart": charts.stacked_columns(daily),
        "breakdowns": breakdowns,
        "top": usage.top_runs(days=days, **scope),
        "compact": charts.compact,
    }


@bp.get("/usage")
@login_required
def my_usage():
    ctx = _dashboard({"user_id": g.user.id, "project_ids": usage.visible_project_ids(g.user)}, _days(),
                     ["project", "model"])
    return render_template("usage/mine.html", **ctx)


@bp.get("/p/<slug>/usage")
@project_perm("usage.view")
def project_usage(slug):
    days = _days()
    ctx = _dashboard({"project_id": g.project.id}, days, ["user", "model", "role"])
    templates = {}
    for t in prompts.list_latest(g.user, slug):
        templates[t["name"]] = prompts.stats(g.user, slug, t["name"])
    return render_template("usage/project.html", **ctx, quality=usage.quality(g.project.id, max(days, 30)),
                           template_stats=templates)


@bp.get("/admin/usage")
@admin_required
def admin_usage():
    ctx = _dashboard({}, _days(), ["user", "project", "model"])
    return render_template("usage/admin.html", **ctx)


@bp.route("/admin/prices", methods=["GET", "POST"])
@admin_required
def admin_prices():
    if request.method == "POST":
        f = request.form
        try:
            if f.get("action") == "delete":
                usage.delete_price(g.user, f.get("model_prefix", ""))
            else:
                usage.set_price(g.user, f.get("model_prefix", "").strip(), f.get("provider", "").strip(), {
                    k: f.get(k) for k in ("input", "output", "cache_read", "cache_write")})
            flash("가격표를 저장했습니다.", "ok")
        except ValidationError as exc:
            flash(str(exc), "error")
        return redirect(url_for("usage.admin_prices"))
    from sagent.core import settings

    return render_template("usage/prices.html", prices=usage.prices(),
                           otel_endpoint=telemetry.endpoint(), otel_available=telemetry.available(),
                           agent_env=settings.get_bool("otel.agent_env"),
                           agent_headers=settings.get_bool("otel.agent_headers"))


@bp.post("/admin/telemetry")
@admin_required
def admin_telemetry():
    from sagent.core import settings

    ep = request.form.get("otel_endpoint", "").strip()
    if ep and not ep.startswith(("http://", "https://")):
        flash("OTLP endpoint 는 http(s):// 로 시작해야 합니다.", "error")
        return redirect(url_for("usage.admin_prices"))
    settings.put("otel.endpoint", ep, g.user)
    settings.put("otel.agent_env", "1" if request.form.get("agent_env") else "0", g.user)
    settings.put("otel.agent_headers", "1" if request.form.get("agent_headers") else "0", g.user)
    hdr = request.form.get("otel_headers", "")
    if request.form.get("clear_headers"):
        settings.put("otel.headers", "", g.user)
    elif hdr.strip():
        settings.put("otel.headers", hdr.strip(), g.user)
    flash("텔레메트리 설정을 저장했습니다.", "ok")
    return redirect(url_for("usage.admin_prices"))


# --- prompt templates ---------------------------------------------------------

@bp.route("/p/<slug>/prompts", methods=["GET", "POST"])
@project_perm("project.view")
def prompt_list(slug):
    can_edit = rbac.role_allows(g.project_role, "prompt.edit")
    if request.method == "POST":
        if not can_edit:
            from sagent.core.errors import Forbidden

            raise Forbidden("프롬프트를 편집할 권한이 없습니다.")
        try:
            r = prompts.save(g.user, slug, request.form.get("name", ""), request.form.get("body", ""),
                             request.form.get("note", ""))
            flash(f"{r['name']} v{r['version']} 을(를) 저장했습니다.", "ok")
            return redirect(url_for("usage.prompt_detail", slug=slug, name=r["name"]))
        except ValidationError as exc:
            flash(str(exc), "error")
    return render_template("usage/prompts.html", items=prompts.list_latest(g.user, slug), can_edit=can_edit,
                           placeholders=prompts.PLACEHOLDERS)


@bp.get("/p/<slug>/prompts/<name>")
@project_perm("project.view")
def prompt_detail(slug, name):
    return render_template(
        "usage/prompt_detail.html", name=name, versions=prompts.versions(g.user, slug, name),
        stats={s["version"]: s for s in prompts.stats(g.user, slug, name)},
        can_edit=rbac.role_allows(g.project_role, "prompt.edit"), placeholders=prompts.PLACEHOLDERS,
    )
