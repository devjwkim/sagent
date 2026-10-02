"""Per-project status summaries for the dashboard (PRD §44) and Overview tab."""
from __future__ import annotations

from sagent import db
from sagent.core import usage


def summary(project_id: int) -> dict:
    loop = db.query_one(
        "SELECT id, loop_name, status, current_node, iteration FROM loop_runs WHERE project_id = ?"
        " ORDER BY CASE WHEN status IN ('RUNNING','PAUSED','WAITING_USER') THEN 0 ELSE 1 END, id DESC LIMIT 1",
        (project_id,))
    run = db.query_one(
        "SELECT id, provider, status, title, started_at, created_at FROM runs WHERE project_id = ?"
        " ORDER BY id DESC LIMIT 1", (project_id,))
    active_runs = db.scalar(
        "SELECT COUNT(*) FROM runs WHERE project_id = ? AND status IN ('QUEUED','RUNNING','WAITING_USER','PAUSED')",
        (project_id,)) or 0
    test = db.query_one(
        "SELECT id, suite, status, passed, failed FROM test_runs WHERE project_id = ? ORDER BY id DESC LIMIT 1",
        (project_id,))
    review = db.query_one(
        "SELECT id, status, verdict, critical, high FROM review_runs WHERE project_id = ? ORDER BY id DESC LIMIT 1",
        (project_id,))
    t = usage.totals(project_id=project_id, days=7)
    return {
        "loop": dict(loop) if loop else None,
        "run": dict(run) if run else None,
        "active_runs": active_runs,
        "test": dict(test) if test else None,
        "review": dict(review) if review else None,
        "tokens_7d": t["tokens"],
        "cost_7d": t["cost"],
    }


def recent_activity(project_id: int, limit: int = 15):
    return db.query(
        "SELECT e.type, e.ts, e.run_id, e.data, u.username FROM events e LEFT JOIN users u ON u.id = e.user_id"
        " WHERE e.project_id = ? AND (e.type LIKE 'run.%' OR e.type LIKE 'loop.%' OR e.type LIKE 'test.%'"
        " OR e.type LIKE 'review.%') ORDER BY e.id DESC LIMIT ?", (project_id, limit))
