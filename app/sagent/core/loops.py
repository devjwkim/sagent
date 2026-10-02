"""Loop engine.

Definitions live in the project's `.sagent/loops.yaml`; every loop run stores
a snapshot of the definition it was started with, so definitions can change
without affecting history. The engine is a state machine advanced by `tick()`
(web worker or CLI). A short lease per loop run keeps two processes from
stepping the same loop at once.

Node outcomes: agent → success|failure, test → success|failure,
review → approve|reject. Edges with `when: always` match any outcome.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone

from sagent import db
from sagent.core import audit, harness, loopdef, projects, rbac, runs, users
from sagent.core.errors import Forbidden, NotFound, SagentError, ValidationError

db.register_schema("""
CREATE TABLE IF NOT EXISTS loop_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    loop_name TEXT NOT NULL,
    definition TEXT NOT NULL,
    task TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'RUNNING',
    current_node TEXT NOT NULL DEFAULT '',
    iteration INTEGER NOT NULL DEFAULT 0,
    context TEXT NOT NULL DEFAULT '{}',
    overrides TEXT NOT NULL DEFAULT '{}',
    pause_requested INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_by INTEGER,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_until TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_loop_runs_project ON loop_runs(project_id, id);
CREATE INDEX IF NOT EXISTS idx_loop_runs_status ON loop_runs(status);

CREATE TABLE IF NOT EXISTS loop_run_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loop_run_id INTEGER NOT NULL REFERENCES loop_runs(id) ON DELETE CASCADE,
    node_key TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    visit INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    outcome TEXT NOT NULL DEFAULT '',
    run_id INTEGER,
    detail TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_lrn_loop ON loop_run_nodes(loop_run_id, id);
""")

LOOP_ACTIVE = ("RUNNING", "PAUSED", "WAITING_USER")
NODE_STATES = ("WAITING", "RUNNING", "SUCCESS", "FAILED", "SKIPPED", "PAUSED", "CANCELLED")
LEASE_SECONDS = 30
_OWNER = f"{socket.gethostname()}:{os.getpid()}"

# review node implementation is injected by sagent.core.reviews
_review_starter = None
_review_outcome = None


def set_review_starter(starter, outcome) -> None:
    global _review_starter, _review_outcome
    _review_starter, _review_outcome = starter, outcome


@dataclass
class LoopRun:
    id: int
    project_id: int
    loop_name: str
    definition: str
    task: str
    status: str
    current_node: str
    iteration: int
    context: str
    overrides: str
    pause_requested: int
    error: str
    created_by: int | None
    lease_owner: str
    lease_until: str | None
    created_at: str
    finished_at: str | None

    @classmethod
    def from_row(cls, row) -> "LoopRun":
        return cls(**{f.name: row[f.name] for f in fields(cls)})

    @property
    def spec(self) -> dict:
        return json.loads(self.definition)

    @property
    def ctx(self) -> dict:
        return json.loads(self.context or "{}")

    @property
    def is_active(self) -> bool:
        return self.status in LOOP_ACTIVE


def _load(loop_run_id: int) -> LoopRun:
    row = db.query_one("SELECT * FROM loop_runs WHERE id = ?", (loop_run_id,))
    if not row:
        raise NotFound("찾을 수 없습니다.")
    return LoopRun.from_row(row)


def _emit(lr: LoopRun, type_: str, data: dict | None = None, user_id: int | None = None) -> None:
    now = db.utcnow()
    db.execute(
        "INSERT INTO events (run_id, project_id, user_id, type, ts, data) VALUES (NULL, ?, ?, ?, ?, ?)",
        (lr.project_id, user_id, type_, now, json.dumps({"loop_run_id": lr.id, **(data or {})})),
    )
    runs._notify({"run_id": None, "loop_run_id": lr.id, "project_id": lr.project_id,
                  "type": type_, "ts": now, "data": data or {}})


def _update(loop_run_id: int, **cols) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)
    db.execute(f"UPDATE loop_runs SET {sets} WHERE id = ?", (*cols.values(), loop_run_id))


# --- definitions ---------------------------------------------------------------

def definitions(project) -> tuple[dict[str, dict], str]:
    data = harness.load(project)["loops.yaml"]
    loops = data.get("loops") if isinstance(data.get("loops"), dict) else {}
    return loops, data.get("default") or (next(iter(loops), ""))


# --- access --------------------------------------------------------------------

def get(actor, loop_run_id: int, perm: str = "project.view"):
    lr = _load(loop_run_id)
    row = db.query_one("SELECT slug FROM projects WHERE id = ?", (lr.project_id,))
    if not row:
        raise NotFound("찾을 수 없습니다.")
    project, role = projects.get(actor, row["slug"], perm)
    return lr, project, role


def list_for_project(actor, slug: str, limit: int = 50) -> list[LoopRun]:
    project, _ = projects.get(actor, slug)
    return [LoopRun.from_row(r) for r in db.query(
        "SELECT * FROM loop_runs WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project.id, limit))]


def node_rows(loop_run_id: int):
    return db.query("SELECT * FROM loop_run_nodes WHERE loop_run_id = ? ORDER BY id", (loop_run_id,))


def node_states(lr: LoopRun) -> dict[str, dict]:
    """Latest state per node key, for the runtime graph."""
    out: dict[str, dict] = {k: {"status": "WAITING", "attempts": 0, "visits": 0} for k in lr.spec["nodes"]}
    for r in node_rows(lr.id):
        st = out.setdefault(r["node_key"], {"status": "WAITING", "attempts": 0, "visits": 0})
        st.update(status=r["status"], outcome=r["outcome"], run_id=r["run_id"], detail=r["detail"],
                  started_at=r["started_at"], finished_at=r["finished_at"])
        st["attempts"] += 1
        st["visits"] = max(st["visits"], r["visit"])
    if lr.status == "PAUSED" and lr.current_node in out and out[lr.current_node]["status"] == "WAITING":
        out[lr.current_node]["status"] = "PAUSED"
    return out


def _require_control(actor, lr: LoopRun) -> None:
    if not rbac.can_control_run(actor, lr.project_id, lr.created_by, "run.control"):
        raise Forbidden("이 Loop 를 제어할 권한이 없습니다.")


# --- start / control -------------------------------------------------------------

def start(actor, slug: str, loop_name: str, task: str, *, provider: str | None = None) -> LoopRun:
    project, _ = projects.get(actor, slug, "run.start")
    if project.is_archived:
        raise ValidationError("보관된 프로젝트에서는 실행할 수 없습니다.")
    loops, default = definitions(project)
    loop_name = loop_name or default
    spec = loops.get(loop_name)
    if spec is None:
        raise ValidationError("알 수 없는 Loop 입니다.")
    errs = loopdef.validate_loop(loop_name, spec)
    if errs:
        raise ValidationError("Loop 정의 오류: " + "; ".join(errs[:5]))
    task = (task or "").strip()
    if not task:
        raise ValidationError("작업 내용을 입력하세요.")
    if len(task) > runs.MAX_PROMPT:
        raise ValidationError("작업 내용이 너무 깁니다.")
    overrides = {"provider": provider} if provider else {}
    ctx = {}
    base = _git_head(project.path)
    if base:
        ctx["base_commit"] = base  # reviews diff against the state before the loop
    lid = db.execute(
        "INSERT INTO loop_runs (project_id, loop_name, definition, task, status, current_node, overrides,"
        " context, created_by, created_at) VALUES (?, ?, ?, ?, 'RUNNING', ?, ?, ?, ?, ?)",
        (project.id, loop_name, json.dumps(spec), task, spec["start"], json.dumps(overrides),
         json.dumps(ctx), actor.id, db.utcnow()),
    )
    lr = _load(lid)
    _emit(lr, "loop.started", {"loop": loop_name}, actor.id)
    audit.record("loop.start", actor, "loop_run", lid, {"loop": loop_name})
    step(lid)
    return _load(lid)


def _git_head(path: str) -> str | None:
    import subprocess

    from sagent.core import gitutil

    try:
        res = gitutil.git(path, "rev-parse", "HEAD", timeout=10, text=True)
    except (OSError, subprocess.TimeoutExpired):
        return None
    head = res.stdout.strip()
    return head if res.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", head) else None


def pause(actor, loop_run_id: int) -> LoopRun:
    lr, _, _ = get(actor, loop_run_id)
    _require_control(actor, lr)
    if lr.status == "RUNNING":
        _update(lr.id, pause_requested=1)
        _emit(lr, "loop.pause_requested", {}, actor.id)
    step(lr.id)
    return _load(lr.id)


def resume(actor, loop_run_id: int, provider: str | None = None) -> LoopRun:
    lr, _, _ = get(actor, loop_run_id)
    _require_control(actor, lr)
    if lr.status not in ("PAUSED", "FAILED"):
        raise ValidationError("일시정지/실패 상태에서만 재개할 수 있습니다.")
    _resume(lr, actor, provider)
    return _load(lr.id)


def _resume(lr: LoopRun, actor, provider: str | None) -> None:
    overrides = json.loads(lr.overrides or "{}")
    if provider:
        overrides["provider"] = provider
    ctx = lr.ctx
    ctx["_manual_resume"] = lr.current_node
    _update(lr.id, status="RUNNING", pause_requested=0, error="", finished_at=None,
            overrides=json.dumps(overrides), context=json.dumps(ctx))
    _emit(lr, "loop.resumed", {"node": lr.current_node, "provider": provider}, actor.id)
    step(lr.id)


def stop(actor, loop_run_id: int) -> LoopRun:
    lr, _, _ = get(actor, loop_run_id)
    _require_control(actor, lr)
    if not lr.is_active:
        return lr
    active = _active_node(lr.id)
    if active is not None and active["run_id"]:
        try:
            runs.stop(actor, active["run_id"], "loop stopped")
        except SagentError:
            pass
    if active is not None:
        _finish_node(active["id"], "CANCELLED", "")
    manual = lr.ctx.get("manual_run_id")
    if manual and runs._load(manual).is_active:
        runs.stop(actor, manual, "loop stopped")
    _update(lr.id, status="CANCELLED", finished_at=db.utcnow())
    _emit(lr, "loop.cancelled", {}, actor.id)
    audit.record("loop.stop", actor, "loop_run", lr.id)
    return _load(lr.id)


def retry_node(actor, loop_run_id: int, provider: str | None = None) -> LoopRun:
    """Re-run the current node (after FAILED / PAUSED), optionally with another agent."""
    lr, _, _ = get(actor, loop_run_id)
    _require_control(actor, lr)
    if lr.status == "RUNNING":
        raise ValidationError("실행 중에는 재시도할 수 없습니다. 먼저 일시정지하세요.")
    _resume(lr, actor, provider)
    return _load(lr.id)


def skip_node(actor, loop_run_id: int) -> LoopRun:
    """Mark the current node SKIPPED with a positive outcome and continue."""
    lr, _, _ = get(actor, loop_run_id)
    _require_control(actor, lr)
    if lr.status == "RUNNING":
        raise ValidationError("실행 중에는 건너뛸 수 없습니다. 먼저 일시정지하세요.")
    spec = lr.spec
    node = spec["nodes"].get(lr.current_node, {})
    outcome = "approve" if node.get("type") == "review" else "success"
    now = db.utcnow()
    db.execute(
        "INSERT INTO loop_run_nodes (loop_run_id, node_key, status, outcome, detail, started_at, finished_at)"
        " VALUES (?, ?, 'SKIPPED', ?, ?, ?, ?)",
        (lr.id, lr.current_node, outcome, f"skipped by {actor.username}", now, now),
    )
    _emit(lr, "loop.node.skipped", {"node": lr.current_node}, actor.id)
    nxt = _next_node(spec, lr.current_node, outcome)
    _update(lr.id, current_node=nxt or lr.current_node)
    if nxt is None:
        _update(lr.id, status="FAILED", error=f"no edge from {lr.current_node} for '{outcome}'")
        return _load(lr.id)
    _resume(_load(lr.id), actor, None)
    return _load(lr.id)


def rerun_from(actor, loop_run_id: int, node_key: str, provider: str | None = None) -> LoopRun:
    lr, _, _ = get(actor, loop_run_id)
    _require_control(actor, lr)
    if lr.status == "RUNNING":
        raise ValidationError("실행 중에는 바꿀 수 없습니다. 먼저 일시정지하세요.")
    if node_key not in lr.spec["nodes"]:
        raise ValidationError("알 수 없는 노드입니다.")
    _update(lr.id, current_node=node_key)
    _emit(lr, "loop.rerun_from", {"node": node_key}, actor.id)
    _resume(_load(lr.id), actor, provider)
    return _load(lr.id)


def take_control(actor, loop_run_id: int, provider: str | None = None):
    """Human-in-the-loop: open an interactive agent session that resumes the
    loop's last agent conversation. The loop waits (WAITING_USER) until
    return_to_automation()."""
    lr, project, _ = get(actor, loop_run_id)
    _require_control(actor, lr)
    if lr.status == "RUNNING":
        raise ValidationError("먼저 Loop 를 일시정지하세요.")
    if lr.status not in ("PAUSED", "FAILED"):
        raise ValidationError("일시정지 또는 실패 상태에서만 직접 개입할 수 있습니다.")
    last = db.query_one(
        "SELECT provider, agent_session_id FROM runs WHERE loop_run_id = ? AND kind = 'agent'"
        " AND agent_session_id != '' ORDER BY id DESC LIMIT 1", (lr.id,))
    prov = provider or (last["provider"] if last else None)
    resume = last["agent_session_id"] if last and last["provider"] == prov else None
    run = runs.start_agent(actor, project.slug, "", mode="interactive", provider=prov, resume_session=resume,
                           loop_run_id=lr.id, node_key="__manual__",
                           title=f"[{lr.loop_name}] manual intervention by {actor.username}")
    ctx = lr.ctx
    ctx["manual_run_id"] = run.id
    _update(lr.id, status="WAITING_USER", context=json.dumps(ctx))
    _emit(lr, "loop.take_control", {"run_id": run.id, "by": actor.username, "resumed": bool(resume)}, actor.id)
    audit.record("loop.take_control", actor, "loop_run", lr.id, {"run_id": run.id})
    return run


def return_to_automation(actor, loop_run_id: int, rerun_from: str | None = None, note: str = "") -> LoopRun:
    lr, _, _ = get(actor, loop_run_id)
    _require_control(actor, lr)
    if lr.status != "WAITING_USER":
        raise ValidationError("직접 개입 중인 Loop 가 아닙니다.")
    ctx = lr.ctx
    manual = ctx.pop("manual_run_id", None)
    if manual:
        run = runs._load(manual)
        if run.is_active:
            runs.stop(actor, manual, "returned to automation")
    ctx["manual"] = ((note or "").strip()[:4000] or
                     "A human made manual changes in the working tree. Inspect `git status` and `git diff` "
                     "before continuing and keep their changes unless they break the task.")
    cols = {"context": json.dumps(ctx), "status": "PAUSED"}
    if rerun_from:
        if rerun_from not in lr.spec["nodes"]:
            raise ValidationError("알 수 없는 노드입니다.")
        cols["current_node"] = rerun_from
    _update(lr.id, **cols)
    _emit(lr, "loop.return_to_automation", {"node": cols.get("current_node", lr.current_node),
                                           "by": actor.username}, actor.id)
    _resume(_load(lr.id), actor, None)
    return _load(lr.id)


# --- engine ----------------------------------------------------------------------

def _active_node(loop_run_id: int):
    return db.query_one(
        "SELECT * FROM loop_run_nodes WHERE loop_run_id = ? AND status = 'RUNNING' ORDER BY id DESC LIMIT 1",
        (loop_run_id,),
    )


def _finish_node(node_row_id: int, status: str, outcome: str, detail: str = "") -> None:
    db.execute(
        "UPDATE loop_run_nodes SET status = ?, outcome = ?, detail = ?, finished_at = ? WHERE id = ?",
        (status, outcome, detail[:4000], db.utcnow(), node_row_id),
    )


def _next_node(spec: dict, node_key: str, outcome: str) -> str | None:
    edges = [e for e in spec.get("edges", []) if e.get("from") == node_key]
    for e in edges:
        if e.get("when", "always") == outcome:
            return e["to"]
    for e in edges:
        if e.get("when", "always") == "always":
            return e["to"]
    return None


def _acquire(loop_run_id: int) -> bool:
    now = datetime.now(timezone.utc)
    until = (now + timedelta(seconds=LEASE_SECONDS)).replace(microsecond=0).isoformat()
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE loop_runs SET lease_owner = ?, lease_until = ? WHERE id = ? AND"
            " (lease_until IS NULL OR lease_until < ? OR lease_owner = ?)",
            (_OWNER, until, loop_run_id, now.replace(microsecond=0).isoformat(), _OWNER),
        )
        return cur.rowcount == 1


def _release(loop_run_id: int) -> None:
    db.execute("UPDATE loop_runs SET lease_until = NULL WHERE id = ? AND lease_owner = ?",
               (loop_run_id, _OWNER))


def step(loop_run_id: int) -> None:
    if not _acquire(loop_run_id):
        return
    try:
        for _ in range(20):  # bounded: instant nodes (skipped tests, end) chain quickly
            if not _step_once(loop_run_id):
                break
    finally:
        _release(loop_run_id)


def _fail(lr: LoopRun, msg: str) -> None:
    _update(lr.id, status="FAILED", error=msg[:500], finished_at=db.utcnow())
    _emit(lr, "loop.failed", {"error": msg[:500], "node": lr.current_node})


def _step_once(loop_run_id: int) -> bool:
    """Advance one transition. Returns True if another step may follow immediately."""
    lr = _load(loop_run_id)
    if lr.status != "RUNNING":
        return False
    spec = lr.spec
    active = _active_node(lr.id)

    if active is not None:
        if not active["run_id"]:
            return False
        child = runs._load(active["run_id"])
        node = spec["nodes"].get(active["node_key"], {})
        timeout = node.get("timeout_sec")
        if child.is_active:
            if timeout and child.started_at:
                started = datetime.fromisoformat(child.started_at)
                if (datetime.now(timezone.utc) - started).total_seconds() > timeout:
                    runs._finalize(child, "FAILED", error=f"timeout after {timeout}s")
                    runs.emit(child, "run.failed", {"error": "timeout"})
                    return True
            return False
        outcome, detail = _outcome(node, child)
        _finish_node(active["id"], "SUCCESS" if outcome in ("success", "approve") else "FAILED",
                     outcome, detail)
        _emit(lr, "loop.node.end", {"node": active["node_key"], "outcome": outcome, "run_id": child.id})
        ctx = lr.ctx
        _update_context(ctx, active["node_key"], node, outcome, child, detail)
        # retries on failure stay on the same node
        if outcome in ("failure", "reject") and node.get("retries"):
            attempts = db.scalar(
                "SELECT COUNT(*) FROM loop_run_nodes WHERE loop_run_id = ? AND node_key = ? AND visit = ?",
                (lr.id, active["node_key"], active["visit"]),
            )
            if attempts <= int(node["retries"]):
                _update(lr.id, context=json.dumps(ctx))
                _emit(lr, "node.retry", {"node": active["node_key"], "attempt": attempts + 1})
                return _launch_node(_load(lr.id), active["node_key"], attempt=attempts + 1,
                                    visit=active["visit"])
        nxt = _next_node(spec, active["node_key"], outcome)
        if nxt is None:
            _update(lr.id, context=json.dumps(ctx))
            _fail(lr, f"no edge from '{active['node_key']}' for outcome '{outcome}'")
            return False
        _update(lr.id, current_node=nxt, context=json.dumps(ctx))
        if _load(lr.id).pause_requested:
            _update(lr.id, status="PAUSED", pause_requested=0)
            _emit(lr, "loop.paused", {"node": nxt})
            return False
        return True

    # no active node: launch current
    if lr.pause_requested:
        _update(lr.id, status="PAUSED", pause_requested=0)
        _emit(lr, "loop.paused", {"node": lr.current_node})
        return False
    key = lr.current_node
    visits = db.scalar(
        "SELECT COALESCE(MAX(visit), 0) FROM loop_run_nodes WHERE loop_run_id = ? AND node_key = ?",
        (lr.id, key),
    ) or 0
    last = db.query_one(
        "SELECT status FROM loop_run_nodes WHERE loop_run_id = ? AND node_key = ? ORDER BY id DESC LIMIT 1",
        (lr.id, key),
    )
    # A manual resume/retry of a failed attempt re-uses the visit; anything
    # else is a fresh visit. Iteration = how often any node was re-entered.
    ctx = lr.ctx
    manual = ctx.pop("_manual_resume", None) == key
    if manual:
        _update(lr.id, context=json.dumps(ctx))
    visit = visits if manual and last is not None and last["status"] in ("FAILED", "CANCELLED") \
        else visits + 1
    iteration = max(lr.iteration, visit - 1)
    if iteration > int(spec.get("max_iterations", 5)):
        _fail(lr, f"max iterations ({spec.get('max_iterations', 5)}) reached")
        return False
    if iteration != lr.iteration:
        _update(lr.id, iteration=iteration)
        _emit(lr, "loop.iteration", {"iteration": iteration, "node": key})
    return _launch_node(_load(lr.id), key, attempt=1, visit=visit)


def _launch_node(lr: LoopRun, key: str, *, attempt: int, visit: int) -> bool:
    spec = lr.spec
    node = spec["nodes"][key]
    ntype = node.get("type")
    now = db.utcnow()
    nid = db.execute(
        "INSERT INTO loop_run_nodes (loop_run_id, node_key, attempt, visit, status, started_at)"
        " VALUES (?, ?, ?, ?, 'RUNNING', ?)",
        (lr.id, key, attempt, visit, now),
    )
    _emit(lr, "loop.node.start", {"node": key, "type": ntype, "attempt": attempt, "visit": visit})

    if ntype == "end":
        _finish_node(nid, "SUCCESS", "success")
        _update(lr.id, status="SUCCESS", finished_at=db.utcnow())
        _emit(lr, "loop.completed", {"iterations": lr.iteration})
        _on_success(lr, node)
        return False

    try:
        actor = users.get(lr.created_by) if lr.created_by else users.SYSTEM
    except NotFound:
        actor = None
    if actor is None or not actor.is_active:
        _finish_node(nid, "FAILED", "failure", "loop owner is no longer active")
        _fail(lr, "loop owner is no longer active")
        return False
    slug = db.scalar("SELECT slug FROM projects WHERE id = ?", (lr.project_id,))
    try:
        if ntype == "agent":
            overrides = json.loads(lr.overrides or "{}")
            template, tname, tver = node.get("prompt") or "{task}", "", None
            if node.get("prompt_ref"):
                from sagent.core import prompts

                tname, tver, template = prompts.resolve(lr.project_id, node["prompt_ref"])
            run = runs.start_agent(
                actor, slug, render_prompt(template, lr),
                provider=node.get("provider") or overrides.get("provider"),
                role=node.get("role", "coding"), loop_run_id=lr.id, node_key=key,
                title=f"[{lr.loop_name}:{key}] {lr.task.splitlines()[0][:80]}",
                prompt_template=tname, prompt_version=tver,
            )
        elif ntype == "test":
            project = projects._get_by_id(lr.project_id)
            suite = node.get("suite", "unit")
            tcfg = harness.load(project)["tests.yaml"].get(suite) or {}
            command = (tcfg.get("command") or "").strip()
            if suite == "e2e" and not tcfg.get("enabled", True):
                command = ""
            if not command:
                _finish_node(nid, "SKIPPED", "success", f"no {suite} command configured")
                _emit(lr, "loop.node.end", {"node": key, "outcome": "success", "skipped": True})
                nxt = _next_node(spec, key, "success")
                if nxt is None:
                    _fail(lr, f"no edge from '{key}' for outcome 'success'")
                    return False
                _update(lr.id, current_node=nxt)
                return True
            from sagent.core import tests as tests_mod

            run = tests_mod.start_suite(actor, slug, suite, command, loop_run_id=lr.id, node_key=key,
                                        timeout_sec=tcfg.get("timeout_sec"))
        elif ntype == "review":
            if _review_starter is None:
                _finish_node(nid, "SKIPPED", "approve", "review not available")
                nxt = _next_node(spec, key, "approve")
                if nxt is None:
                    _fail(lr, f"no edge from '{key}' for outcome 'approve'")
                    return False
                _update(lr.id, current_node=nxt)
                return True
            overrides = json.loads(lr.overrides or "{}")
            run = _review_starter(actor, slug, loop_run_id=lr.id, node_key=key, task=lr.task,
                                  provider=node.get("provider") or overrides.get("review_provider"),
                                  base_ref=lr.ctx.get("base_commit") or "HEAD")
        else:
            raise ValidationError(f"unknown node type {ntype}")
    except SagentError as exc:
        _finish_node(nid, "FAILED", "failure", str(exc))
        _fail(lr, f"{key}: {exc}")
        return False
    db.execute("UPDATE loop_run_nodes SET run_id = ? WHERE id = ?", (run.id, nid))
    if not run.is_active:  # failed to launch
        return True
    return False


def _outcome(node: dict, child) -> tuple[str, str]:
    if node.get("type") == "review":
        if _review_outcome is None:
            return "reject", "review not available"
        return _review_outcome(child)
    ok = child.status == "SUCCESS"
    return ("success" if ok else "failure"), (child.summary if ok else (child.error or child.summary))


def _last_message(run_id: int, limit: int = 8000) -> str:
    row = db.query_one(
        "SELECT data FROM events WHERE run_id = ? AND type = 'agent.message' ORDER BY id DESC LIMIT 1",
        (run_id,),
    )
    if not row:
        return ""
    return (json.loads(row["data"]).get("text") or "")[:limit]


def _update_context(ctx: dict, key: str, node: dict, outcome: str, child, detail: str) -> None:
    ntype = node.get("type")
    if ntype == "agent":
        ctx.pop("manual", None)
    if ntype == "agent" and outcome == "success":
        text = _last_message(child.id) or child.summary
        ctx.setdefault("outputs", {})[key] = text[:8000]
        if key == "plan" or node.get("produces") == "plan":
            ctx["plan"] = text[:8000]
        ctx.pop("failure", None)
    elif ntype == "test":
        if outcome == "failure":
            try:
                screen = (runs.run_dir(child.id) / "screen.txt").read_text(errors="replace")
            except OSError:
                screen = ""
            tail = "\n".join(screen.strip().splitlines()[-80:])
            ctx["failure"] = f"{node.get('suite', 'test')} failed (`{child.command}`):\n{tail}"[:8000]
        else:
            ctx.pop("failure", None)
    elif ntype == "review":
        if outcome == "reject":
            ctx["review"] = (detail or "")[:8000]
        else:
            ctx.pop("review", None)


def render_prompt(template: str, lr: LoopRun) -> str:
    ctx = lr.ctx
    sections = {
        "{task}": lr.task,
        "{plan}": f"Plan from the previous step:\n{ctx['plan']}\n\n" if ctx.get("plan") else "",
        "{failure}": f"Previous verification failed:\n{ctx['failure']}\n\n" if ctx.get("failure") else "",
        "{review}": f"Code review requested changes:\n{ctx['review']}\n\n" if ctx.get("review") else "",
    }
    out = template
    for k, v in sections.items():
        out = out.replace(k, v)
    if ctx.get("manual"):
        out += f"\n\nNote from the operator after a manual intervention:\n{ctx['manual']}"
    return out.strip()


def _on_success(lr: LoopRun, node: dict) -> None:
    target = node.get("lifecycle")
    if target and target in projects.LIFECYCLES:
        db.execute("UPDATE projects SET lifecycle = ?, updated_at = ? WHERE id = ?",
                   (target, db.utcnow(), lr.project_id))


def tick() -> int:
    rows = db.query("SELECT id FROM loop_runs WHERE status = 'RUNNING' ORDER BY id")
    for r in rows:
        try:
            step(r["id"])
        except Exception as exc:
            print(f"sagent loops: loop {r['id']}: {exc}", file=sys.stderr)
    return len(rows)


def wait(loop_run_id: int, timeout: float | None = None, poll: float = 1.0) -> LoopRun:
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        runs.tick()
        step(loop_run_id)
        lr = _load(loop_run_id)
        if lr.status != "RUNNING":
            return lr
        if deadline is not None and time.monotonic() > deadline:
            return lr
        time.sleep(poll)
