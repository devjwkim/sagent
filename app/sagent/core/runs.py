"""Run manager: agent / command runs on top of tmux, with a normalised event store.

SQLite is the authoritative logical state; tmux holds the real processes.
`tick()` ingests new agent output and finalises finished runs; it is safe to
call from several processes (the web worker and the CLI) at the same time.
"""
from __future__ import annotations

import json
import re
import sys
import time
import uuid
from dataclasses import dataclass, fields
from pathlib import Path

from sagent import agents, db
from sagent.agents.base import RunSpec
from sagent.core import audit, harness, projects, rbac
from sagent.core.errors import Forbidden, NotFound, ValidationError
from sagent.runtime import tmux

db.register_schema("""
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'agent',
    mode TEXT NOT NULL DEFAULT 'auto',
    provider TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'coding',
    title TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    command TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'QUEUED',
    tmux_session TEXT NOT NULL DEFAULT '',
    agent_session_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    created_by INTEGER,
    loop_run_id INTEGER,
    node_key TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    outcome_ok INTEGER,
    summary TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    events_offset INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    duration_ms INTEGER,
    prompt_template TEXT NOT NULL DEFAULT '',
    prompt_version INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    last_event_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_loop ON runs(loop_run_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    project_id INTEGER,
    user_id INTEGER,
    type TEXT NOT NULL,
    ts TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
""")

ACTIVE = ("QUEUED", "RUNNING", "WAITING_USER", "PAUSED")
FINAL = ("SUCCESS", "FAILED", "CANCELLED")
MAX_PROMPT = 200_000

_runs_root: Path | None = None
_listeners: list = []  # callables(event_dict) — e.g. OTel exporter
_env_providers: list = []  # callables(run, project) -> extra env for the process


def configure(runs_root: Path) -> None:
    global _runs_root
    _runs_root = Path(runs_root)
    _runs_root.mkdir(parents=True, exist_ok=True)


def add_listener(fn) -> None:
    if fn not in _listeners:
        _listeners.append(fn)


def root() -> Path:
    if _runs_root is None:
        raise RuntimeError("runs not configured")
    return _runs_root


def add_env_provider(fn) -> None:
    if fn not in _env_providers:
        _env_providers.append(fn)


def run_dir(run_id: int) -> Path:
    if _runs_root is None:
        raise RuntimeError("runs not configured")
    return _runs_root / str(int(run_id))


@dataclass
class Run:
    id: int
    project_id: int
    kind: str
    mode: str
    provider: str
    role: str
    title: str
    prompt: str
    command: str
    status: str
    tmux_session: str
    agent_session_id: str
    model: str
    created_by: int | None
    loop_run_id: int | None
    node_key: str
    exit_code: int | None
    outcome_ok: int | None
    summary: str
    error: str
    events_offset: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float | None
    duration_ms: int | None
    prompt_template: str
    prompt_version: int | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    last_event_at: str | None

    @classmethod
    def from_row(cls, row) -> "Run":
        return cls(**{f.name: row[f.name] for f in fields(cls)})

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


def _load(run_id: int) -> Run:
    row = db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not row:
        raise NotFound("찾을 수 없습니다.")
    return Run.from_row(row)


def emit(run: Run, type_: str, data: dict | None = None, user_id: int | None = None) -> None:
    now = db.utcnow()
    payload = json.dumps(data or {}, ensure_ascii=False)
    db.execute(
        "INSERT INTO events (run_id, project_id, user_id, type, ts, data) VALUES (?, ?, ?, ?, ?, ?)",
        (run.id, run.project_id, user_id, type_, now, payload),
    )
    db.execute("UPDATE runs SET last_event_at = ? WHERE id = ?", (now, run.id))
    _notify({"run_id": run.id, "project_id": run.project_id, "type": type_, "ts": now, "data": data or {}})


def _notify(event: dict) -> None:
    for fn in list(_listeners):
        try:
            fn(event)
        except Exception:  # listeners must never break the run loop
            pass


# --- access ----------------------------------------------------------------

def get(actor, run_id: int, perm: str = "project.view"):
    """Return (run, project, role). Invisible runs raise NotFound."""
    run = _load(run_id)
    row = db.query_one("SELECT slug FROM projects WHERE id = ?", (run.project_id,))
    if not row:
        raise NotFound("찾을 수 없습니다.")
    project, role = projects.get(actor, row["slug"], perm)
    return run, project, role


def list_for_project(actor, slug: str, limit: int = 50, loop_run_id: int | None = None) -> list[Run]:
    project, _ = projects.get(actor, slug)
    sql = "SELECT * FROM runs WHERE project_id = ?"
    params: list = [project.id]
    if loop_run_id is not None:
        sql += " AND loop_run_id = ?"
        params.append(loop_run_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(min(max(limit, 1), 500))
    return [Run.from_row(r) for r in db.query(sql, tuple(params))]


def list_events(actor, run_id: int, after_id: int = 0, limit: int = 500):
    get(actor, run_id)
    return db.query(
        "SELECT * FROM events WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?",
        (run_id, after_id, limit),
    )


# --- starting runs -----------------------------------------------------------

def _agent_settings(project, role: str) -> dict:
    cfg = harness.load(project)
    agent_cfg = (cfg["harness.yaml"].get("agents") or {}).get(role) or {}
    return agent_cfg if isinstance(agent_cfg, dict) else {}


def _insert(project, actor, **cols) -> Run:
    now = db.utcnow()
    cols = {"project_id": project.id, "created_by": actor.id, "created_at": now, **cols}
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    rid = db.execute(f"INSERT INTO runs ({names}) VALUES ({marks})", tuple(cols.values()))
    return _load(rid)


def _launch(run: Run, project, provider: str, cmd: list[str], *, stdin: Path | None = None,
            tty: bool = False, env: dict[str, str] | None = None) -> Run:
    """Start the wrapper in tmux. The tmux command line is fixed; the real
    command, stdin and env travel in spec.json (tmux parses ';' in argv)."""
    d = run_dir(run.id)
    d.mkdir(parents=True, exist_ok=True)
    name = tmux.session_name(project.slug, run.id)
    extra: dict[str, str] = {}
    for fn in _env_providers:
        try:
            extra.update(fn(run, project) or {})
        except Exception:  # an env provider must never block a run
            pass
    spec = {
        "provider": provider,
        "cmd": cmd,
        "stdin": str(stdin) if stdin else None,
        "tty": tty,
        "env": {**extra, "SAGENT_RUN_ID": str(run.id), "SAGENT_PROJECT": project.slug, **(env or {})},
    }
    spec_file = d / "spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    argv = [sys.executable, "-m", "sagent.runtime.wrapper", "--spec", str(spec_file)]
    try:
        tmux.start(name, Path(project.path), argv, log_path=d / "terminal.log")
    except tmux.TmuxError as exc:
        db.execute(
            "UPDATE runs SET status = 'FAILED', error = ?, finished_at = ? WHERE id = ?",
            (str(exc)[:500], db.utcnow(), run.id),
        )
        emit(run, "run.failed", {"error": str(exc)[:500]})
        return _load(run.id)
    db.execute(
        "UPDATE runs SET status = 'RUNNING', tmux_session = ?, started_at = ? WHERE id = ?",
        (name, db.utcnow(), run.id),
    )
    run = _load(run.id)
    emit(run, "run.started", {"kind": run.kind, "provider": run.provider, "mode": run.mode,
                              "node": run.node_key or None}, run.created_by)
    return run


def _check_project_active(project) -> None:
    if project.is_archived:
        raise ValidationError("보관된 프로젝트에서는 실행할 수 없습니다.")


def start_agent(
    actor,
    slug: str,
    prompt: str,
    *,
    provider: str | None = None,
    role: str = "coding",
    mode: str = "auto",
    title: str = "",
    resume_session: str | None = None,
    loop_run_id: int | None = None,
    node_key: str = "",
    permission_mode: str | None = None,
    prompt_template: str = "",
    prompt_version: int | None = None,
) -> Run:
    project, _ = projects.get(actor, slug, "run.start")
    _check_project_active(project)
    if mode not in ("auto", "interactive"):
        raise ValidationError("mode 는 auto 또는 interactive 입니다.")
    prompt = (prompt or "").strip()
    if mode == "auto" and not prompt:
        raise ValidationError("프롬프트를 입력하세요.")
    if len(prompt) > MAX_PROMPT:
        raise ValidationError("프롬프트가 너무 깁니다.")
    settings = _agent_settings(project, role)
    provider = provider or settings.get("provider") or project.primary_agent
    if provider not in agents.names():
        raise ValidationError("지원하지 않는 에이전트입니다.")
    adapter = agents.get(provider)
    if not adapter.check_installed():
        raise ValidationError(f"{adapter.label} 이(가) 서버에 설치되어 있지 않습니다.")
    if resume_session and not _is_safe_id(resume_session):
        raise ValidationError("잘못된 세션 ID 입니다.")

    run = _insert(
        project, actor, kind="agent", mode=mode, provider=provider, role=role,
        title=(title or (prompt.splitlines()[0] if prompt else f"{provider} interactive"))[:200],
        prompt=prompt[:4000], model=str(settings.get("model") or ""),
        loop_run_id=loop_run_id, node_key=node_key,
        prompt_template=prompt_template, prompt_version=prompt_version,
    )
    d = run_dir(run.id)
    d.mkdir(parents=True, exist_ok=True)
    prompt_file = d / "prompt.md"
    prompt_file.write_text(prompt, encoding="utf-8")
    session_id = None
    if provider == "claude" and not resume_session:
        session_id = str(uuid.uuid4())
        db.execute("UPDATE runs SET agent_session_id = ? WHERE id = ?", (session_id, run.id))
    policy = adapter.permission_policy(harness.load(project)["harness.yaml"].get("permissions") or {})
    spec = RunSpec(
        cwd=Path(project.path), prompt_file=prompt_file, mode=mode,
        resume_session=resume_session, session_id=session_id,
        model=settings.get("model") or None,
        permission_mode=permission_mode or settings.get("permission_mode") or policy.get("permission_mode"),
        extra={"max_budget_usd": settings.get("max_budget_usd"),
               "allowed_tools": policy.get("allowed_tools"),
               "disallowed_tools": policy.get("disallowed_tools")},
    )
    agent_argv = adapter.build_command(spec)
    audit.record("run.start", actor, "run", run.id, {"provider": provider, "mode": mode})
    if mode == "auto":
        return _launch(run, project, provider, agent_argv, stdin=prompt_file)
    if prompt:  # first message of the interactive session; "--" so it is never an option
        agent_argv += ["--", prompt]
    return _launch(run, project, "shell", agent_argv, tty=True)


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _is_safe_id(value: str) -> bool:
    # leading alphanumeric: an id can never be parsed as a CLI option
    return bool(_SAFE_ID.match(value or ""))


def start_command(
    actor,
    slug: str,
    command: str,
    *,
    title: str = "",
    loop_run_id: int | None = None,
    node_key: str = "",
    env: dict[str, str] | None = None,
) -> Run:
    """Run a project command (from .sagent/tests.yaml) through bash in tmux.
    The command text comes from maintainer-controlled config, never from a form."""
    project, _ = projects.get(actor, slug, "run.start")
    _check_project_active(project)
    command = (command or "").strip()
    if not command:
        raise ValidationError("실행할 명령이 없습니다.")
    run = _insert(
        project, actor, kind="command", mode="auto", provider="shell", role="test",
        title=(title or command)[:200], command=command[:4000],
        loop_run_id=loop_run_id, node_key=node_key,
    )
    audit.record("run.command", actor, "run", run.id, {"title": run.title})
    return _launch(run, project, "shell", ["bash", "-lc", command], env=env)


# --- control -----------------------------------------------------------------

def _require_control(actor, run: Run, kind: str) -> None:
    if not rbac.can_control_run(actor, run.project_id, run.created_by, kind):
        raise Forbidden("이 Run 을 제어할 권한이 없습니다.")


def stop(actor, run_id: int, reason: str = "stopped by user") -> Run:
    run, _, _ = get(actor, run_id)
    _require_control(actor, run, "run.control")
    if not run.is_active:
        return run
    _finalize(run, "CANCELLED", error=reason)
    emit(run, "run.cancelled", {"by": getattr(actor, "username", None)}, actor.id)
    audit.record("run.stop", actor, "run", run.id)
    return _load(run.id)


def send_input(actor, run_id: int, text: str, enter: bool = True) -> None:
    run, _, _ = get(actor, run_id)
    _require_control(actor, run, "terminal.input")
    if not run.is_active or not run.tmux_session:
        raise ValidationError("실행 중인 Run 이 아닙니다.")
    if len(text) > 20000:
        raise ValidationError("입력이 너무 깁니다.")
    tmux.send_text(run.tmux_session, text, enter=enter)
    emit(run, "terminal.input", {"length": len(text), "by": actor.username}, actor.id)
    audit.record("run.input", actor, "run", run.id, {"length": len(text)})


def send_key(actor, run_id: int, key: str) -> None:
    run, _, _ = get(actor, run_id)
    _require_control(actor, run, "terminal.input")
    if not run.is_active or not run.tmux_session:
        raise ValidationError("실행 중인 Run 이 아닙니다.")
    try:
        tmux.send_key(run.tmux_session, key)
    except tmux.TmuxError as exc:
        raise ValidationError(str(exc)) from None
    emit(run, "terminal.key", {"key": key, "by": actor.username}, actor.id)


def terminal(actor, run_id: int, lines: int = 2000) -> tuple[Run, str]:
    run, _, _ = get(actor, run_id, "terminal.view")
    if run.is_active and run.tmux_session and tmux.exists(run.tmux_session):
        return run, tmux.capture(run.tmux_session, lines)
    screen = run_dir(run.id) / "screen.txt"
    return run, screen.read_text(encoding="utf-8", errors="replace") if screen.exists() else ""


def read_prompt(actor, run_id: int) -> str:
    run, _, _ = get(actor, run_id, "terminal.view")
    p = run_dir(run.id) / "prompt.md"
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else run.prompt


# --- monitor -----------------------------------------------------------------

def _ingest(run: Run) -> None:
    if run.kind != "agent" or run.mode != "auto":
        return
    path = run_dir(run.id) / "agent.jsonl"
    if not path.exists():
        return
    size = path.stat().st_size
    if size <= run.events_offset:
        return
    with open(path, "rb") as fh:
        fh.seek(run.events_offset)
        chunk = fh.read(min(size - run.events_offset, 8 * 1024 * 1024))
    end = chunk.rfind(b"\n")
    if end < 0:
        return
    chunk = chunk[: end + 1]
    new_offset = run.events_offset + len(chunk)
    adapter = agents.get(run.provider)
    pending: list[tuple[str, dict]] = []
    updates: dict = {}
    usage_add = [0, 0, 0, 0]
    cost = None
    for raw in chunk.splitlines():
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        sid = adapter.session_id(obj)
        if sid and _is_safe_id(sid):
            updates["agent_session_id"] = sid
        u = adapter.usage(obj)
        if u:
            usage_add = [usage_add[0] + u.input_tokens, usage_add[1] + u.output_tokens,
                         usage_add[2] + u.cache_read_tokens, usage_add[3] + u.cache_write_tokens]
            if u.cost_usd is not None:
                cost = (cost or 0) + float(u.cost_usd)
            if u.model:
                updates["model"] = u.model
        out = adapter.outcome(obj)
        if out:
            updates["outcome_ok"] = int(out[0])
            updates["summary"] = out[1][:500]
        for ev in adapter.parse(obj):
            pending.append((ev.type, ev.data))
    now = db.utcnow()
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE runs SET events_offset = ? WHERE id = ? AND events_offset = ?",
            (new_offset, run.id, run.events_offset),
        )
        if cur.rowcount == 0:
            return  # another process ingested this chunk
        for type_, data in pending:
            conn.execute(
                "INSERT INTO events (run_id, project_id, type, ts, data) VALUES (?, ?, ?, ?, ?)",
                (run.id, run.project_id, type_, now, json.dumps(data, ensure_ascii=False)),
            )
        conn.execute(
            "UPDATE runs SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ?,"
            " cache_read_tokens = cache_read_tokens + ?, cache_write_tokens = cache_write_tokens + ?,"
            " cost_usd = CASE WHEN ? IS NULL THEN cost_usd ELSE COALESCE(cost_usd, 0) + ? END,"
            " last_event_at = ? WHERE id = ?",
            (*usage_add, cost, cost, now, run.id),
        )
        if updates:
            cols = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*updates.values(), run.id))
    for type_, data in pending:
        _notify({"run_id": run.id, "project_id": run.project_id, "type": type_, "ts": now, "data": data})


def _finalize(run: Run, status: str, *, exit_code: int | None = None, error: str = "",
              duration_ms: int | None = None) -> bool:
    """Move a run to a final state exactly once. Returns False if already final."""
    screen = ""
    if run.tmux_session:
        try:
            screen = tmux.capture(run.tmux_session, 5000)
        except tmux.TmuxError:
            screen = ""
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE runs SET status = ?, exit_code = COALESCE(?, exit_code), error = ?,"
            " duration_ms = COALESCE(?, duration_ms), finished_at = ? WHERE id = ? AND status IN"
            " ('QUEUED','RUNNING','WAITING_USER','PAUSED')",
            (status, exit_code, error[:500], duration_ms, db.utcnow(), run.id),
        )
        changed = cur.rowcount > 0
    if not changed:
        return False
    d = run_dir(run.id)
    if screen and d.exists():
        (d / "screen.txt").write_text(screen, encoding="utf-8")
    if run.tmux_session:
        try:
            tmux.kill(run.tmux_session)
        except tmux.TmuxError:
            pass
    return True


def _check_finished(run: Run) -> None:
    d = run_dir(run.id)
    exit_file = d / "exit.json"
    if exit_file.exists():
        try:
            info = json.loads(exit_file.read_text())
        except (OSError, json.JSONDecodeError):
            return
        _ingest(_load(run.id))  # make sure the tail is processed
        run = _load(run.id)
        code = info.get("code")
        ok = code == 0
        if run.kind == "agent" and run.mode == "auto" and run.outcome_ok is not None:
            ok = ok and bool(run.outcome_ok)
        status = "SUCCESS" if ok else "FAILED"
        err = "" if ok else (run.summary if run.outcome_ok == 0 else f"exit code {code}")
        if _finalize(run, status, exit_code=code, error=err, duration_ms=info.get("duration_ms")):
            emit(run, "run.completed" if ok else "run.failed", {"exit_code": code})
        return
    st = tmux.status(run.tmux_session) if run.tmux_session else None
    if st is None:
        if _finalize(run, "FAILED", error="tmux session disappeared"):
            emit(run, "run.failed", {"error": "orphaned"})
    elif st["dead"]:
        code = st["exit"]
        if _finalize(run, "SUCCESS" if code == 0 else "FAILED", exit_code=code,
                     error="" if code == 0 else f"exit code {code}"):
            emit(run, "run.completed" if code == 0 else "run.failed", {"exit_code": code})


def tick() -> int:
    """Process all active runs once. Returns the number of active runs seen."""
    rows = db.query("SELECT * FROM runs WHERE status = 'RUNNING' ORDER BY id")
    for row in rows:
        run = Run.from_row(row)
        try:
            _ingest(run)
            _check_finished(_load(run.id))
        except Exception as exc:  # keep monitoring other runs
            print(f"sagent monitor: run {run.id}: {exc}", file=sys.stderr)
    return len(rows)


def purge_old_logs(days: int | None = None) -> int:
    """Delete run directories (terminal logs, prompts, agent output) of runs
    that finished more than `days` ago. DB rows and usage stay."""
    import shutil
    from datetime import datetime, timedelta, timezone

    from sagent.core import settings

    days = days if days is not None else settings.get_int("runs.log_retention_days")
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    n = 0
    for row in db.query("SELECT id FROM runs WHERE finished_at IS NOT NULL AND finished_at < ?", (cutoff,)):
        d = run_dir(row["id"])
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    for row in db.query("SELECT id FROM test_runs WHERE finished_at IS NOT NULL AND finished_at < ?", (cutoff,)):
        d = root() / f"test-{int(row['id'])}"
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    return n


def recover() -> None:
    """On startup: runs whose tmux session vanished while sagent was down."""
    tick()


def wait(run_id: int, timeout: float | None = None, poll: float = 1.0) -> Run:
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        run = _load(run_id)
        if not run.is_active:
            return run
        if deadline is not None and time.monotonic() > deadline:
            return run
        tick()
        run = _load(run_id)
        if not run.is_active:
            return run
        time.sleep(poll)
