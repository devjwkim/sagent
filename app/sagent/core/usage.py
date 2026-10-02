"""LLMOps: usage records, cost estimation and quality metrics.

Every finished run becomes one `usage_records` row attributed to the user who
started it. Headless runs report tokens/cost from the agent's JSON stream; for
interactive Claude sessions the transcript (~/.claude/projects/*/<session>.jsonl)
is read once the run ends. When the agent does not report cost, it is
estimated from the admin-editable `model_prices` table (an estimate only).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sagent import db
from sagent.core import projects, runs
from sagent.core.errors import Forbidden, ValidationError

db.register_schema("""
CREATE TABLE IF NOT EXISTS usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    user_id INTEGER,
    project_id INTEGER,
    loop_run_id INTEGER,
    kind TEXT NOT NULL DEFAULT 'agent',
    role TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    cost_source TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    prompt_template TEXT NOT NULL DEFAULT '',
    prompt_version INTEGER,
    day TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_day ON usage_records(day);
CREATE INDEX IF NOT EXISTS idx_usage_project ON usage_records(project_id, day);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_records(user_id, day);

CREATE TABLE IF NOT EXISTS model_prices (
    model_prefix TEXT PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT '',
    input_per_mtok REAL NOT NULL DEFAULT 0,
    output_per_mtok REAL NOT NULL DEFAULT 0,
    cache_read_per_mtok REAL NOT NULL DEFAULT 0,
    cache_write_per_mtok REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
""")

# Seed prices (USD per million tokens, Anthropic first-party list prices).
# Cache write = 1.25x input (5 minute TTL), cache read = 0.1x input unless the
# model has its own published rate. Admins can edit or add rows (e.g. OpenAI
# models used through Codex) — costs computed from this table are estimates.
DEFAULT_PRICES = [
    # prefix, provider, input, output, cache_read, cache_write
    ("claude-fable-5-1", "claude", 10.0, 50.0, 0.25, 12.5),
    ("claude-fable-5", "claude", 10.0, 50.0, 1.0, 12.5),
    ("claude-opus-5-5", "claude", 4.0, 20.0, 0.20, 5.0),
    ("claude-opus-5", "claude", 5.0, 25.0, 0.5, 6.25),
    ("claude-opus-4", "claude", 5.0, 25.0, 0.5, 6.25),
    ("claude-sonnet-5", "claude", 2.0, 10.0, 0.2, 2.5),
    ("claude-sonnet-4", "claude", 3.0, 15.0, 0.3, 3.75),
    ("claude-haiku-4-5", "claude", 1.0, 5.0, 0.1, 1.25),
]

MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,99}$")


def seed_prices() -> None:
    now = db.utcnow()
    with db.connect() as conn:
        for prefix, provider, i, o, cr, cw in DEFAULT_PRICES:
            conn.execute(
                "INSERT OR IGNORE INTO model_prices (model_prefix, provider, input_per_mtok, output_per_mtok,"
                " cache_read_per_mtok, cache_write_per_mtok, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (prefix, provider, i, o, cr, cw, now),
            )


def prices():
    return db.query("SELECT * FROM model_prices ORDER BY provider, model_prefix")


def set_price(actor, prefix: str, provider: str, values: dict) -> None:
    if not actor.is_admin:
        raise Forbidden("관리자만 가격표를 바꿀 수 있습니다.")
    if not MODEL_RE.match(prefix or ""):
        raise ValidationError("모델 접두어 형식이 올바르지 않습니다.")
    nums = []
    for k in ("input", "output", "cache_read", "cache_write"):
        try:
            v = float(values.get(k) or 0)
        except ValueError:
            raise ValidationError("가격은 숫자여야 합니다.") from None
        if v < 0 or v > 10_000:
            raise ValidationError("가격 범위가 올바르지 않습니다.")
        nums.append(v)
    db.execute(
        "INSERT INTO model_prices (model_prefix, provider, input_per_mtok, output_per_mtok, cache_read_per_mtok,"
        " cache_write_per_mtok, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(model_prefix) DO UPDATE SET"
        " provider = excluded.provider, input_per_mtok = excluded.input_per_mtok,"
        " output_per_mtok = excluded.output_per_mtok, cache_read_per_mtok = excluded.cache_read_per_mtok,"
        " cache_write_per_mtok = excluded.cache_write_per_mtok, updated_at = excluded.updated_at",
        (prefix, provider[:20], *nums, db.utcnow()),
    )
    from sagent.core import audit

    audit.record("prices.update", actor, "model_price", prefix, {"provider": provider})


def delete_price(actor, prefix: str) -> None:
    if not actor.is_admin:
        raise Forbidden("관리자만 가격표를 바꿀 수 있습니다.")
    db.execute("DELETE FROM model_prices WHERE model_prefix = ?", (prefix,))


def estimate_cost(model: str, i: int, o: int, cr: int, cw: int) -> float | None:
    if not model:
        return None
    best = None
    for row in db.query("SELECT * FROM model_prices"):
        if model.startswith(row["model_prefix"]) and (best is None or len(row["model_prefix"]) > len(best["model_prefix"])):
            best = row
    if best is None:
        return None
    return (i * best["input_per_mtok"] + o * best["output_per_mtok"] + cr * best["cache_read_per_mtok"]
            + cw * best["cache_write_per_mtok"]) / 1_000_000


# --- transcripts (interactive Claude sessions) ---------------------------------

def claude_transcript(session_id: str) -> Path | None:
    if not session_id or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", session_id):
        return None
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    if not base.is_dir():
        return None
    for p in base.glob(f"*/{session_id}.jsonl"):
        if p.is_file():
            return p
    return None


def transcript_usage(path: Path) -> dict:
    """Sum assistant usage from a Claude Code transcript (dedup by message id)."""
    seen: dict[str, dict] = {}
    model = ""
    with open(path, "rb") as fh:
        for raw in fh:
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict) or obj.get("type") != "assistant":
                continue
            msg = obj.get("message") or {}
            u = msg.get("usage")
            if not isinstance(u, dict):
                continue
            mid = msg.get("id") or obj.get("uuid") or str(len(seen))
            seen[mid] = u  # later chunks of the same message carry the final numbers
            model = msg.get("model") or model
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for u in seen.values():
        tot["input"] += int(u.get("input_tokens") or 0)
        tot["output"] += int(u.get("output_tokens") or 0)
        tot["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
        tot["cache_write"] += int(u.get("cache_creation_input_tokens") or 0)
    tot["model"] = model if model != "<synthetic>" else ""
    return tot


# --- recording ------------------------------------------------------------------

def record_run(run_id: int) -> None:
    run = runs._load(run_id)
    if run.is_active:
        return
    if db.scalar("SELECT 1 FROM usage_records WHERE run_id = ?", (run_id,)):
        return
    i, o, cr, cw = run.input_tokens, run.output_tokens, run.cache_read_tokens, run.cache_write_tokens
    model = run.model
    if run.kind == "agent" and run.mode == "interactive" and run.provider == "claude":
        path = claude_transcript(run.agent_session_id)
        if path:
            t = transcript_usage(path)
            i, o, cr, cw = t["input"], t["output"], t["cache_read"], t["cache_write"]
            model = t["model"] or model
            db.execute(
                "UPDATE runs SET input_tokens = ?, output_tokens = ?, cache_read_tokens = ?,"
                " cache_write_tokens = ?, model = ? WHERE id = ?", (i, o, cr, cw, model, run_id))
    cost, source = run.cost_usd, "agent"
    if cost is None:
        cost = estimate_cost(model, i, o, cr, cw)
        source = "estimate" if cost is not None else ""
        if cost is not None:
            db.execute("UPDATE runs SET cost_usd = ? WHERE id = ?", (cost, run_id))
    duration = run.duration_ms
    if duration is None and run.started_at and run.finished_at:
        duration = int((datetime.fromisoformat(run.finished_at) - datetime.fromisoformat(run.started_at))
                       .total_seconds() * 1000)
    ts = run.finished_at or db.utcnow()
    db.execute(
        "INSERT OR IGNORE INTO usage_records (run_id, user_id, project_id, loop_run_id, kind, role, provider, model,"
        " status, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, cost_source,"
        " duration_ms, prompt_template, prompt_version, day, ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run.id, run.created_by, run.project_id, run.loop_run_id, run.kind, run.role, run.provider, model,
         run.status, i, o, cr, cw, cost or 0, source, duration or 0, run.prompt_template or "",
         run.prompt_version, ts[:10], ts),
    )


def _on_event(ev: dict) -> None:
    if ev.get("type") in ("run.completed", "run.failed", "run.cancelled") and ev.get("run_id"):
        record_run(ev["run_id"])


runs.add_listener(_on_event)


# --- queries ----------------------------------------------------------------------

RANGES = {"1": 1, "7": 7, "30": 30, "90": 90}


def since_day(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")


def _where(project_id=None, user_id=None, days: int = 7, project_ids: list[int] | None = None,
           alias: str = ""):
    a = f"{alias}." if alias else ""
    sql, params = f" WHERE {a}day >= ?", [since_day(days)]
    if project_id is not None:
        sql += f" AND {a}project_id = ?"
        params.append(project_id)
    if user_id is not None:
        sql += f" AND {a}user_id = ?"
        params.append(user_id)
    if project_ids is not None:
        if not project_ids:
            sql += " AND 0"
        else:
            sql += f" AND {a}project_id IN ({','.join('?' * len(project_ids))})"
            params += project_ids
    return sql, params


def totals(**scope) -> dict:
    w, p = _where(**scope)
    row = db.query_one(
        "SELECT COUNT(*) AS runs, COALESCE(SUM(input_tokens),0) AS input, COALESCE(SUM(output_tokens),0) AS output,"
        " COALESCE(SUM(cache_read_tokens),0) AS cache_read, COALESCE(SUM(cache_write_tokens),0) AS cache_write,"
        " COALESCE(SUM(cost_usd),0) AS cost, COALESCE(SUM(duration_ms),0) AS duration,"
        " COALESCE(SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END),0) AS ok,"
        " COALESCE(SUM(CASE WHEN cost_source='estimate' THEN 1 ELSE 0 END),0) AS estimated,"
        " COALESCE(SUM(CASE WHEN kind='agent' THEN duration_ms ELSE 0 END),0) AS agent_ms"
        f" FROM usage_records{w}", tuple(p))
    d = dict(row)
    d["tokens"] = d["input"] + d["output"] + d["cache_read"] + d["cache_write"]
    d["success_rate"] = (d["ok"] / d["runs"]) if d["runs"] else None
    return d


def by_day(days: int = 7, **scope) -> list[dict]:
    w, p = _where(days=days, **scope)
    rows = db.query(
        "SELECT day, provider, SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens) AS tokens,"
        f" SUM(cost_usd) AS cost, SUM(duration_ms) AS ms FROM usage_records{w} AND kind = 'agent'"
        " GROUP BY day, provider", tuple(p))
    start = datetime.strptime(since_day(days), "%Y-%m-%d")
    out = []
    for n in range(days):
        day = (start + timedelta(days=n)).strftime("%Y-%m-%d")
        entry = {"day": day, "claude": 0, "codex": 0, "cost": 0.0, "ms": 0}
        for r in rows:
            if r["day"] == day:
                entry[r["provider"] if r["provider"] in ("claude", "codex") else "claude"] += r["tokens"] or 0
                entry["cost"] += r["cost"] or 0
                entry["ms"] += r["ms"] or 0
        out.append(entry)
    return out


def breakdown(dimension: str, days: int = 7, limit: int = 10, **scope) -> list[dict]:
    col = {"user": "user_id", "project": "project_id", "model": "model", "provider": "provider",
           "role": "role", "template": "prompt_template"}[dimension]
    w, p = _where(days=days, **scope)
    rows = db.query(
        f"SELECT {col} AS key, COUNT(*) AS runs,"
        " SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens) AS tokens,"
        " SUM(cost_usd) AS cost, SUM(duration_ms) AS ms,"
        " SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) AS ok"
        f" FROM usage_records{w} GROUP BY {col} ORDER BY tokens DESC LIMIT ?", (*p, limit))
    names: dict = {}
    if dimension == "user":
        names = {r["id"]: r["username"] for r in db.query("SELECT id, username FROM users")}
    elif dimension == "project":
        names = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM projects")}
    out = []
    for r in rows:
        if dimension in ("user", "project"):
            label = names.get(r["key"], "system" if dimension == "user" else "-")
        else:
            label = r["key"] or "(unknown)"
        out.append({"key": r["key"], "label": label, "runs": r["runs"], "tokens": r["tokens"] or 0,
                    "cost": r["cost"] or 0, "ms": r["ms"] or 0,
                    "success_rate": (r["ok"] / r["runs"]) if r["runs"] else None})
    return out


def top_runs(days: int = 7, limit: int = 10, **scope):
    w, p = _where(days=days, alias="u", **scope)
    return db.query(
        f"SELECT u.*, r.title FROM usage_records u JOIN runs r ON r.id = u.run_id{w}"
        " ORDER BY u.cost_usd DESC, (u.input_tokens + u.output_tokens) DESC LIMIT ?", (*p, limit))


def quality(project_id: int, days: int = 30) -> dict:
    """Loop / review / test health — the LLMOps quality side of usage."""
    since = since_day(days)
    lr = db.query_one(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) AS ok,"
        " SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) AS failed, AVG(iteration) AS avg_iter"
        " FROM loop_runs WHERE project_id = ? AND substr(created_at, 1, 10) >= ? AND status NOT IN"
        " ('RUNNING','PAUSED')", (project_id, since))
    cost_per_success = db.scalar(
        "SELECT SUM(u.cost_usd) / NULLIF(COUNT(DISTINCT CASE WHEN l.status='SUCCESS' THEN l.id END), 0)"
        " FROM loop_runs l LEFT JOIN usage_records u ON u.loop_run_id = l.id"
        " WHERE l.project_id = ? AND substr(l.created_at, 1, 10) >= ?", (project_id, since))
    nodes = db.query(
        "SELECT n.node_key, COUNT(*) AS n, SUM(CASE WHEN n.status='FAILED' THEN 1 ELSE 0 END) AS failed,"
        " SUM(CASE WHEN n.attempt > 1 THEN 1 ELSE 0 END) AS retries"
        " FROM loop_run_nodes n JOIN loop_runs l ON l.id = n.loop_run_id"
        " WHERE l.project_id = ? AND substr(n.started_at, 1, 10) >= ? GROUP BY n.node_key ORDER BY failed DESC",
        (project_id, since))
    rv = db.query_one(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN verdict='reject' THEN 1 ELSE 0 END) AS rejected,"
        " SUM(critical) AS critical, SUM(high) AS high FROM review_runs"
        " WHERE project_id = ? AND status != 'RUNNING' AND substr(created_at, 1, 10) >= ?", (project_id, since))
    tr = db.query_one(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) AS ok,"
        " SUM(flaky) AS flaky FROM test_runs WHERE project_id = ? AND status != 'RUNNING'"
        " AND substr(created_at, 1, 10) >= ?", (project_id, since))

    def rate(a, b):
        return (a or 0) / b if b else None

    return {
        "loops": lr["n"] or 0, "loop_success": rate(lr["ok"], lr["n"]), "loop_failed": lr["failed"] or 0,
        "avg_iterations": lr["avg_iter"], "cost_per_success": cost_per_success,
        "nodes": [dict(r) | {"fail_rate": rate(r["failed"], r["n"])} for r in nodes],
        "reviews": rv["n"] or 0, "reject_rate": rate(rv["rejected"], rv["n"]),
        "critical": rv["critical"] or 0, "high": rv["high"] or 0,
        "tests": tr["n"] or 0, "test_pass_rate": rate(tr["ok"], tr["n"]), "flaky": tr["flaky"] or 0,
    }


def visible_project_ids(actor) -> list[int]:
    return [p.id for p, role in projects.list_for(actor, include_archived=True)
            if role in ("viewer", "developer", "maintainer", "owner")]
