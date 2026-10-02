"""Test suites (unit / lint / typecheck / e2e).

sagent does not run tests itself: it runs the project's own commands from
`.sagent/tests.yaml` in tmux, then aggregates the results:
- Playwright: `--reporter=line,html,json` into the run directory
  (HTML report served as-is, JSON parsed into test cases + steps).
- pytest: `--junitxml` into the run directory.
- anything else: `junit:` path in tests.yaml (relative to the project), or
  just the exit code.
"""
from __future__ import annotations

import json
import os
import shlex
import xml.etree.ElementTree as ET
from dataclasses import dataclass, fields
from pathlib import Path

from sagent import db
from sagent.core import audit, harness, projects, rbac, runs
from sagent.core.errors import NotFound, ValidationError

db.register_schema("""
CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    loop_run_id INTEGER,
    suite TEXT NOT NULL,
    command TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'RUNNING',
    framework TEXT NOT NULL DEFAULT '',
    total INTEGER NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    flaky INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    has_report INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_test_runs_project ON test_runs(project_id, id);
CREATE INDEX IF NOT EXISTS idx_test_runs_run ON test_runs(run_id);

CREATE TABLE IF NOT EXISTS test_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    test_run_id INTEGER NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    file TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    duration_ms INTEGER,
    error TEXT NOT NULL DEFAULT '',
    steps TEXT NOT NULL DEFAULT '[]',
    attachments TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_test_cases_run ON test_cases(test_run_id);
""")

SUITES = ("unit", "lint", "typecheck", "e2e")
MAX_RESULT_FILE = 20 * 1024 * 1024


@dataclass
class TestRun:
    id: int
    project_id: int
    run_id: int | None
    loop_run_id: int | None
    suite: str
    command: str
    status: str
    framework: str
    total: int
    passed: int
    failed: int
    skipped: int
    flaky: int
    duration_ms: int | None
    has_report: int
    created_by: int | None
    created_at: str
    finished_at: str | None

    @classmethod
    def from_row(cls, row) -> "TestRun":
        return cls(**{f.name: row[f.name] for f in fields(cls)})


def _detect(command: str) -> str:
    c = command.lower()
    if "playwright" in c and " test" in c:
        return "playwright"
    if "pytest" in c:
        return "pytest"
    return ""


def _augment(command: str, framework: str, out: Path) -> tuple[str, dict[str, str]]:
    """Add reporter flags so results land in the run directory."""
    env: dict[str, str] = {}
    if framework == "playwright":
        env.update({
            "PLAYWRIGHT_HTML_REPORT": str(out / "playwright" / "report"),
            "PLAYWRIGHT_HTML_OUTPUT_DIR": str(out / "playwright" / "report"),
            "PLAYWRIGHT_HTML_OPEN": "never",
            "PLAYWRIGHT_JSON_OUTPUT_NAME": str(out / "playwright" / "results.json"),
            "PLAYWRIGHT_JSON_OUTPUT_FILE": str(out / "playwright" / "results.json"),
        })
        command += f" --reporter=line,html,json --output={shlex.quote(str(out / 'playwright' / 'test-results'))}"
    elif framework == "pytest":
        command += f" --junitxml={shlex.quote(str(out / 'junit.xml'))}"
    return command, env


def start_suite(actor, slug: str, suite: str, command: str | None = None, *, loop_run_id=None,
                node_key: str = "", timeout_sec=None):
    """Start a suite. `command` defaults to tests.yaml; callers never pass form input here."""
    if suite not in SUITES:
        raise ValidationError("알 수 없는 테스트 종류입니다.")
    project, _ = projects.get(actor, slug, "run.start")
    cfg = harness.load(project)["tests.yaml"].get(suite) or {}
    if command is None:
        command = (cfg.get("command") or "").strip()
        if suite == "e2e" and not cfg.get("enabled", True):
            command = ""
    if not command:
        raise ValidationError(f"{suite} 명령이 tests.yaml 에 없습니다.")
    framework = _detect(command)
    # Insert the command run first to get its directory, then launch.
    tid = db.execute(
        "INSERT INTO test_runs (project_id, loop_run_id, suite, command, framework, created_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project.id, loop_run_id, suite, command[:2000], framework, actor.id, db.utcnow()),
    )
    out = output_dir(tid)
    out.mkdir(parents=True, exist_ok=True)
    full, env = _augment(command, framework, out)
    env["SAGENT_TEST_OUTPUT"] = str(out)
    run = runs.start_command(actor, slug, full, title=f"{suite}: {command}"[:200],
                             loop_run_id=loop_run_id, node_key=node_key, env=env)
    db.execute("UPDATE test_runs SET run_id = ? WHERE id = ?", (run.id, tid))
    if not run.is_active:
        finalize_for_run(run.id)
    audit.record("test.start", actor, "test_run", tid, {"suite": suite})
    return run


def output_dir(test_run_id: int) -> Path:
    return runs.root() / f"test-{int(test_run_id)}"


def _read_limited(p: Path) -> bytes | None:
    if not p.is_file() or p.stat().st_size > MAX_RESULT_FILE:
        return None
    return p.read_bytes()


def parse_junit(data: bytes) -> list[dict]:
    if b"<!DOCTYPE" in data[:2000] or b"<!ENTITY" in data:
        return []  # refuse entity tricks
    root = ET.fromstring(data)
    cases = []
    for tc in root.iter("testcase"):
        status, error = "passed", ""
        for tag in ("failure", "error"):
            el = tc.find(tag)
            if el is not None:
                status = "failed"
                error = ((el.get("message") or "") + "\n" + (el.text or "")).strip()
        if tc.find("skipped") is not None:
            status = "skipped"
        try:
            dur = int(float(tc.get("time") or 0) * 1000)
        except ValueError:
            dur = None
        cases.append({
            "file": tc.get("file") or tc.get("classname") or "",
            "title": tc.get("name") or "?",
            "project": "",
            "status": status,
            "duration_ms": dur,
            "error": error[:8000],
            "steps": [],
            "attachments": [],
        })
    return cases


def parse_playwright(data: bytes) -> list[dict]:
    report = json.loads(data)
    cases: list[dict] = []

    def steps_of(result: dict) -> list[dict]:
        out = []
        for s in result.get("steps") or []:
            out.append({"title": s.get("title", ""), "duration_ms": s.get("duration"),
                        "error": ((s.get("error") or {}).get("message") or "")[:500]})
        return out[:100]

    def walk(suite: dict, file: str) -> None:
        file = suite.get("file") or file
        for spec in suite.get("specs") or []:
            for test in spec.get("tests") or []:
                results = test.get("results") or []
                last = results[-1] if results else {}
                outcome = test.get("status")  # expected | unexpected | flaky | skipped
                status = {"expected": "passed", "unexpected": "failed", "flaky": "flaky",
                          "skipped": "skipped"}.get(outcome, last.get("status") or "unknown")
                err = last.get("error") or {}
                atts = [{"name": a.get("name"), "path": a.get("path"), "type": a.get("contentType")}
                        for a in last.get("attachments") or [] if a.get("path")]
                cases.append({
                    "file": file,
                    "title": spec.get("title", "?"),
                    "project": test.get("projectName") or "",
                    "status": status,
                    "duration_ms": last.get("duration"),
                    "error": ((err.get("message") or "") + "\n" + (err.get("stack") or "")).strip()[:8000],
                    "steps": steps_of(last),
                    "attachments": atts[:20],
                })
        for child in suite.get("suites") or []:
            walk(child, file)

    for s in report.get("suites") or []:
        walk(s, "")
    return cases


def finalize_for_run(run_id: int) -> None:
    row = db.query_one("SELECT * FROM test_runs WHERE run_id = ? AND status = 'RUNNING'", (run_id,))
    if not row:
        return
    tr = TestRun.from_row(row)
    run = runs._load(run_id)
    if run.is_active:
        return
    out = output_dir(tr.id)
    cases: list[dict] = []
    has_report = 0
    try:
        if tr.framework == "playwright":
            data = _read_limited(out / "playwright" / "results.json")
            if data:
                cases = parse_playwright(data)
            has_report = int((out / "playwright" / "report" / "index.html").is_file())
        elif tr.framework == "pytest":
            data = _read_limited(out / "junit.xml")
            if data:
                cases = parse_junit(data)
        else:
            project = projects._get_by_id(tr.project_id)
            junit = (harness.load(project)["tests.yaml"].get(tr.suite) or {}).get("junit")
            if junit:
                p = Path(os.path.realpath(Path(project.path) / junit))
                if str(p).startswith(project.path + os.sep):
                    data = _read_limited(p)
                    if data:
                        cases = parse_junit(data)
    except (ET.ParseError, json.JSONDecodeError, OSError, ValueError):
        cases = []
    counts = {"passed": 0, "failed": 0, "skipped": 0, "flaky": 0}
    with db.connect() as conn:
        for c in cases:
            key = c["status"] if c["status"] in counts else "failed"
            counts[key] += 1
            conn.execute(
                "INSERT INTO test_cases (test_run_id, file, title, project, status, duration_ms, error, steps,"
                " attachments) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tr.id, c["file"][:300], c["title"][:500], c["project"][:100], c["status"], c["duration_ms"],
                 c["error"], json.dumps(c["steps"]), json.dumps(c["attachments"])),
            )
        status = "SUCCESS" if run.status == "SUCCESS" else ("CANCELLED" if run.status == "CANCELLED" else "FAILED")
        conn.execute(
            "UPDATE test_runs SET status = ?, total = ?, passed = ?, failed = ?, skipped = ?, flaky = ?,"
            " duration_ms = ?, has_report = ?, finished_at = ? WHERE id = ?",
            (status, len(cases), counts["passed"], counts["failed"], counts["skipped"], counts["flaky"],
             run.duration_ms, has_report, db.utcnow(), tr.id),
        )
    runs.emit(run, "test.end", {"suite": tr.suite, "status": status, "total": len(cases), **counts})


def _on_event(ev: dict) -> None:
    if ev.get("type") in ("run.completed", "run.failed", "run.cancelled") and ev.get("run_id"):
        finalize_for_run(ev["run_id"])


runs.add_listener(_on_event)


# --- queries -------------------------------------------------------------------

def list_for_project(actor, slug: str, limit: int = 50) -> list[TestRun]:
    project, _ = projects.get(actor, slug)
    return [TestRun.from_row(r) for r in db.query(
        "SELECT * FROM test_runs WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project.id, limit))]


def get(actor, test_run_id: int, perm: str = "project.view"):
    row = db.query_one("SELECT * FROM test_runs WHERE id = ?", (test_run_id,))
    if not row:
        raise NotFound("찾을 수 없습니다.")
    tr = TestRun.from_row(row)
    slug = db.scalar("SELECT slug FROM projects WHERE id = ?", (tr.project_id,))
    project, role = projects.get(actor, slug, perm)
    return tr, project, role


def cases(test_run_id: int):
    return db.query(
        "SELECT * FROM test_cases WHERE test_run_id = ? ORDER BY CASE status WHEN 'failed' THEN 0"
        " WHEN 'flaky' THEN 1 ELSE 2 END, file, id", (test_run_id,))


def safe_artifact(actor, test_run_id: int, rel: str) -> Path:
    """Resolve a file inside the test run's output dir (report or attachment)."""
    tr, _, _ = get(actor, test_run_id, "terminal.view")
    base = Path(os.path.realpath(output_dir(tr.id)))
    target = Path(os.path.realpath(base / rel))
    if target != base and base not in target.parents:
        raise NotFound("파일을 찾을 수 없습니다.")
    if not target.is_file():
        raise NotFound("파일을 찾을 수 없습니다.")
    return target


def analyze_failures(actor, test_run_id: int):
    """Ask the test-analysis agent to explain the failures (read-only prompt)."""
    tr, project, role = get(actor, test_run_id, "run.start")
    if not rbac.role_allows(role, "run.start"):
        raise ValidationError("권한이 없습니다.")
    failed = [c for c in cases(tr.id) if c["status"] in ("failed", "flaky")][:20]
    if not failed and tr.status == "SUCCESS":
        raise ValidationError("실패한 테스트가 없습니다.")
    parts = [f"The `{tr.suite}` test suite failed (command: `{tr.command}`).",
             "Analyse the failures below. Explain the most likely root cause for each and propose a fix.",
             "Do NOT modify any files — this is an analysis only.", ""]
    for c in failed:
        parts.append(f"### {c['file']} › {c['title']}")
        parts.append((c["error"] or "(no error message)")[:3000])
        parts.append("")
    if not failed and tr.run_id:
        screen = runs.run_dir(tr.run_id) / "screen.txt"
        if screen.exists():
            parts.append("Terminal output (tail):\n" + "\n".join(screen.read_text(errors="replace").splitlines()[-120:]))
    return runs.start_agent(actor, project.slug, "\n".join(parts), role="test_analysis",
                            title=f"AI analysis of {tr.suite} test run #{tr.id}")


def update_snapshots(actor, test_run_id: int):
    """Accept new visual-regression baselines: re-run Playwright with
    --update-snapshots. Baselines are committed project files, so this needs
    harness.edit (maintainer+)."""
    tr, project, role = get(actor, test_run_id)
    if not rbac.role_allows(role, "harness.edit"):
        from sagent.core.errors import Forbidden

        raise Forbidden("스냅샷 기준을 바꿀 권한이 없습니다.")
    if tr.framework != "playwright":
        raise ValidationError("Playwright 테스트에서만 사용할 수 있습니다.")
    return start_suite(actor, project.slug, tr.suite, tr.command + " --update-snapshots")
