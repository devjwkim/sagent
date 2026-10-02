from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from sagent import agents, db
from sagent.core import rbac, reviews, runs, users
from sagent.core.errors import ValidationError
from sagent.web.security import project_perm

bp = Blueprint("reviews", __name__)


def _rr_in_project(review_run_id: int):
    rr, project, _ = reviews.get(g.user, review_run_id)
    if project.id != g.project.id:
        abort(404)
    return rr


@bp.get("/p/<slug>/reviews")
@project_perm("project.view")
def review_list(slug):
    return render_template(
        "reviews/list.html",
        items=reviews.list_for_project(g.user, slug, limit=100),
        can_run=rbac.role_allows(g.project_role, "run.start"),
        providers=agents.names(),
        names={u.id: u.username for u in users.list_all()},
    )


@bp.post("/p/<slug>/reviews")
@project_perm("run.start")
def review_start(slug):
    try:
        run = reviews.start(g.user, slug, base_ref=(request.form.get("base_ref") or "HEAD").strip(),
                            provider=request.form.get("provider") or None)
    except ValidationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("reviews.review_list", slug=slug))
    rid = db.scalar("SELECT id FROM review_runs WHERE run_id = ?", (run.id,))
    return redirect(url_for("reviews.review_detail", slug=slug, review_run_id=rid))


@bp.get("/p/<slug>/reviews/<int:review_run_id>")
@project_perm("project.view")
def review_detail(slug, review_run_id):
    rr = _rr_in_project(review_run_id)
    run = runs._load(rr.run_id) if rr.run_id else None
    return render_template("reviews/detail.html", rr=rr, issues=reviews.issues(rr.id), run=run,
                           names={u.id: u.username for u in users.list_all()})
