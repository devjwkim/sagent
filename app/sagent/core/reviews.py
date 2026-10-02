"""AI PR review.

Input is more than a diff: changed files, untracked files, the latest test /
lint / typecheck results and the project rules are bundled into the prompt.
The reviewer runs read-only (Claude `--permission-mode plan`, Codex
`--sandbox read-only`) and must answer with a JSON verdict. Any issue whose
severity is listed in review.yaml `block_on` forces a reject.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, fields
from pathlib import Path

from sagent import agents, db
from sagent.core import audit, gitutil, harness, loops, projects, runs
from sagent.core.errors import NotFound, ValidationError

db.register_schema("""
CREATE TABLE IF NOT EXISTS review_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    loop_run_id INTEGER,
    provider TEXT NOT NULL DEFAULT '',
    base_ref TEXT NOT NULL DEFAULT 'HEAD',
    status TEXT NOT NULL DEFAULT 'RUNNING',
    verdict TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    files_changed INTEGER NOT NULL DEFAULT 0,
    diff_bytes INTEGER NOT NULL DEFAULT 0,
    critical INTEGER NOT NULL DEFAULT 0,
    high INTEGER NOT NULL DEFAULT 0,
    medium INTEGER NOT NULL DEFAULT 0,
    low INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_runs_project ON review_runs(project_id, id);
CREATE INDEX IF NOT EXISTS idx_review_runs_run ON review_runs(run_id);

CREATE TABLE IF NOT EXISTS review_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_run_id INTEGER NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    severity TEXT NOT NULL,
    file TEXT NOT NULL DEFAULT '',
    line INTEGER,
    category TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    suggested_fix TEXT NOT NULL DEFAULT '',
    confidence REAL
);
CREATE INDEX IF NOT EXISTS idx_review_issues_run ON review_issues(review_run_id);
""")

SEVERITIES = ("critical", "high", "medium", "low")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^-]{0,99}$")
MAX_UNTRACKED_FILE = 20 * 1024
MAX_UNTRACKED_FILES = 20
READ_ONLY_MODE = {"claude": "plan", "codex": "read-only"}

PROMPT_HEADER = """You are a strict senior code reviewer. Review the change below for correctness bugs,
security problems, data loss, broken tests, and violations of the project rules.
Do NOT modify any files and do not run commands that change state: this is a read-only review.
You may read files in the repository for context.

Answer with ONLY one JSON object (no prose before or after), exactly in this shape:
{"verdict": "approve" | "reject",
 "summary": "<2-4 sentences>",
 "issues": [{"severity": "critical|high|medium|low", "file": "<path>", "line": <int or null>,
             "category": "<bug|security|performance|test|style|rules|other>",
             "reason": "<what is wrong and why>", "suggested_fix": "<concrete fix>",
             "confidence": <0.0-1.0>}]}
Reject when there is any critical or high issue. Report each problem once.
"""


@dataclass
class ReviewRun:
    id: int
    project_id: int
    run_id: int | None
    loop_run_id: int | None
    provider: str
    base_ref: str
    status: str
    verdict: str
    summary: str
    files_changed: int
    diff_bytes: int
    critical: int
    high: int
    medium: int
    low: int
    created_by: int | None
    created_at: str
    finished_at: str | None

    @classmethod
    def from_row(cls, row) -> "ReviewRun":
        return cls(**{f.name: row[f.name] for f in fields(cls)})


# --- input collection ----------------------------------------------------------

def _git(path: str, *args: str, limit: int | None = None) -> str:
    try:
        res = gitutil.git(path, *args)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    out = res.stdout if res.returncode == 0 else b""
    if limit is not None:
        out = out[:limit]
    return out.decode("utf-8", errors="replace")


def collect(project, base_ref: str = "HEAD") -> dict:
    if not REF_RE.match(base_ref or ""):
        raise ValidationError("잘못된 git ref 입니다.")
    cfg = harness.load(project)
    review_cfg = cfg["review.yaml"]
    max_bytes = int(review_cfg.get("max_diff_kb") or 200) * 1024
    path = project.path
    if not (Path(path) / ".git").exists():
        raise ValidationError("git 저장소가 아니어서 리뷰할 diff 가 없습니다.")
    if not _git(path, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}").strip():
        raise ValidationError(f"git ref '{base_ref}' 를 찾을 수 없습니다.")
    name_status = _git(path, "diff", "--name-status", base_ref, "--")
    diff = _git(path, "diff", "--no-color", "--no-ext-diff", base_ref, "--", limit=max_bytes + 1)
    truncated = len(diff.encode()) > max_bytes
    untracked = [f for f in _git(path, "ls-files", "--others", "--exclude-standard").splitlines() if f]
    untracked_blobs = []
    for rel in untracked[:MAX_UNTRACKED_FILES]:
        p = Path(path) / rel
        try:
            if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_UNTRACKED_FILE:
                continue
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        untracked_blobs.append((rel, text))
    changed = [line.split("\t")[-1] for line in name_status.splitlines() if line.strip()] + untracked
    # latest result per suite
    suites = []
    for suite in ("lint", "typecheck", "unit", "e2e"):
        row = db.query_one(
            "SELECT * FROM test_runs WHERE project_id = ? AND suite = ? AND status != 'RUNNING'"
            " ORDER BY id DESC LIMIT 1", (project.id, suite))
        if row:
            failed = db.query("SELECT file, title, substr(error, 1, 600) AS error FROM test_cases"
                              " WHERE test_run_id = ? AND status = 'failed' LIMIT 10", (row["id"],))
            suites.append({"suite": suite, "status": row["status"], "passed": row["passed"],
                           "failed": row["failed"], "failures": [dict(f) for f in failed]})
    h = cfg["harness.yaml"]
    return {
        "base_ref": base_ref,
        "name_status": name_status,
        "diff": diff[:max_bytes],
        "truncated": truncated,
        "untracked": untracked_blobs,
        "changed": changed,
        "tests": suites,
        "rules": list(h.get("rules") or []) + [f"Forbidden: {x}" for x in h.get("forbidden") or []],
        "block_on": [s for s in review_cfg.get("block_on", ["critical", "high"]) if s in SEVERITIES],
    }


def build_prompt(bundle: dict, task: str = "") -> str:
    parts = [PROMPT_HEADER]
    if task:
        parts += ["## Task the change implements", task.strip(), ""]
    if bundle["rules"]:
        parts += ["## Project rules", *[f"- {r}" for r in bundle["rules"]], ""]
    if bundle["tests"]:
        parts.append("## Latest verification results")
        for s in bundle["tests"]:
            parts.append(f"- {s['suite']}: {s['status']} (passed {s['passed']}, failed {s['failed']})")
            for f in s["failures"]:
                parts.append(f"  - FAILED {f['file']} › {f['title']}: {(f['error'] or '').strip()[:300]}")
        parts.append("")
    parts += [f"## Changed files (vs {bundle['base_ref']})", bundle["name_status"].strip() or "(none tracked)", ""]
    parts += ["## Diff", "```diff", bundle["diff"].rstrip() or "(empty)", "```"]
    if bundle["truncated"]:
        parts.append("(diff truncated — read the files directly for the rest)")
    for rel, text in bundle["untracked"]:
        parts += [f"## New file: {rel}", "```", text.rstrip(), "```"]
    return "\n".join(parts) + "\n"


# --- start --------------------------------------------------------------------------

def _provider(project, override: str | None) -> str:
    if override:
        return override
    cfg = harness.load(project)
    return (cfg["review.yaml"].get("provider")
            or ((cfg["harness.yaml"].get("agents") or {}).get("review") or {}).get("provider")
            or project.primary_agent)


def start(actor, slug: str, *, base_ref: str = "HEAD", provider: str | None = None, task: str = "",
          loop_run_id: int | None = None, node_key: str = ""):
    project, _ = projects.get(actor, slug, "run.start")
    provider = _provider(project, provider)
    if provider not in agents.names():
        raise ValidationError("지원하지 않는 리뷰 에이전트입니다.")
    bundle = collect(project, base_ref or "HEAD")
    if not bundle["diff"].strip() and not bundle["untracked"]:
        raise ValidationError("리뷰할 변경 사항이 없습니다.")
    rid = db.execute(
        "INSERT INTO review_runs (project_id, loop_run_id, provider, base_ref, files_changed, diff_bytes,"
        " created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project.id, loop_run_id, provider, bundle["base_ref"], len(bundle["changed"]),
         len(bundle["diff"].encode()), actor.id, db.utcnow()),
    )
    run = runs.start_agent(
        actor, slug, build_prompt(bundle, task), provider=provider, role="review",
        loop_run_id=loop_run_id, node_key=node_key,
        title=f"AI review ({provider}) vs {bundle['base_ref'][:12]}",
        permission_mode=READ_ONLY_MODE.get(provider),
    )
    db.execute("UPDATE review_runs SET run_id = ? WHERE id = ?", (run.id, rid))
    audit.record("review.start", actor, "review_run", rid, {"provider": provider})
    if not run.is_active:
        finalize_for_run(run.id)
    return run


def _start_for_loop(actor, slug, *, loop_run_id, node_key, task, provider=None, base_ref="HEAD"):
    try:
        return start(actor, slug, provider=provider, task=task, loop_run_id=loop_run_id,
                     node_key=node_key, base_ref=base_ref)
    except ValidationError as exc:
        if "변경 사항이 없습니다" in str(exc):
            raise ValidationError("review: no changes to review") from None
        raise


# --- result parsing ----------------------------------------------------------------

def _final_text(run) -> str:
    path = runs.run_dir(run.id) / "agent.jsonl"
    if not path.exists():
        return ""
    adapter = agents.get(run.provider)
    text = ""
    with open(path, "rb") as fh:
        for raw in fh:
            if len(raw) > runs.MAX_LINE:
                continue
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(obj, dict):
                t = adapter.final_text(obj)
                if t:
                    text = t
    return text


def extract_json(text: str) -> dict | None:
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            data = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "verdict" in data:
            return data
    return None


def _clean_issue(i: dict) -> dict | None:
    if not isinstance(i, dict):
        return None
    sev = str(i.get("severity", "")).lower()
    if sev not in SEVERITIES:
        sev = "medium"
    try:
        line = int(i["line"]) if i.get("line") not in (None, "") else None
    except (TypeError, ValueError):
        line = None
    try:
        conf = max(0.0, min(1.0, float(i.get("confidence")))) if i.get("confidence") is not None else None
    except (TypeError, ValueError):
        conf = None
    return {
        "severity": sev, "file": str(i.get("file") or "")[:300], "line": line,
        "category": str(i.get("category") or "")[:50], "reason": str(i.get("reason") or "")[:4000],
        "suggested_fix": str(i.get("suggested_fix") or "")[:4000], "confidence": conf,
    }


def finalize_for_run(run_id: int) -> None:
    row = db.query_one("SELECT * FROM review_runs WHERE run_id = ? AND status = 'RUNNING'", (run_id,))
    if not row:
        return
    rr = ReviewRun.from_row(row)
    run = runs._load(run_id)
    if run.is_active:
        return
    data = extract_json(_final_text(run)) if run.status == "SUCCESS" else None
    issues = [x for x in (_clean_issue(i) for i in (data or {}).get("issues") or []) if x][:200]
    counts = {s: sum(1 for i in issues if i["severity"] == s) for s in SEVERITIES}
    project = projects._get_by_id(rr.project_id)
    block_on = [s for s in harness.load(project)["review.yaml"].get("block_on", ["critical", "high"])
                if s in SEVERITIES]
    if data is None:
        status, verdict = "FAILED", "reject"
        summary = run.error or "리뷰 결과(JSON)를 해석하지 못했습니다."
    else:
        status = "SUCCESS"
        verdict = "approve" if str(data.get("verdict")).lower() == "approve" else "reject"
        if any(counts[s] for s in block_on):
            verdict = "reject"
        summary = str(data.get("summary") or "")[:4000]
    with db.connect() as conn:
        for i in issues:
            conn.execute(
                "INSERT INTO review_issues (review_run_id, severity, file, line, category, reason,"
                " suggested_fix, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rr.id, i["severity"], i["file"], i["line"], i["category"], i["reason"],
                 i["suggested_fix"], i["confidence"]),
            )
        conn.execute(
            "UPDATE review_runs SET status = ?, verdict = ?, summary = ?, critical = ?, high = ?, medium = ?,"
            " low = ?, finished_at = ? WHERE id = ?",
            (status, verdict, summary, counts["critical"], counts["high"], counts["medium"],
             counts["low"], db.utcnow(), rr.id),
        )
    runs.emit(run, "review.end", {"verdict": verdict, **counts})


def issues_text(review_run_id: int) -> str:
    """Compact text handed back to the coding agent after a reject."""
    rr = db.query_one("SELECT summary FROM review_runs WHERE id = ?", (review_run_id,))
    lines = [rr["summary"]] if rr and rr["summary"] else []
    for i in db.query("SELECT * FROM review_issues WHERE review_run_id = ? ORDER BY CASE severity"
                      " WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END",
                      (review_run_id,)):
        loc = f"{i['file']}:{i['line']}" if i["line"] else i["file"]
        lines.append(f"- [{i['severity']}] {loc} {i['reason']}"
                     + (f" → fix: {i['suggested_fix']}" if i["suggested_fix"] else ""))
    return "\n".join(lines)


def _on_event(ev: dict) -> None:
    if ev.get("type") in ("run.completed", "run.failed", "run.cancelled") and ev.get("run_id"):
        finalize_for_run(ev["run_id"])


def _loop_outcome(run) -> tuple[str, str]:
    finalize_for_run(run.id)
    row = db.query_one("SELECT id, verdict FROM review_runs WHERE run_id = ?", (run.id,))
    if not row:
        return "reject", run.error or "review missing"
    return ("approve" if row["verdict"] == "approve" else "reject"), issues_text(row["id"])


runs.add_listener(_on_event)
loops.set_review_starter(_start_for_loop, _loop_outcome)


# --- queries -------------------------------------------------------------------------

def list_for_project(actor, slug: str, limit: int = 50) -> list[ReviewRun]:
    project, _ = projects.get(actor, slug)
    return [ReviewRun.from_row(r) for r in db.query(
        "SELECT * FROM review_runs WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project.id, limit))]


def get(actor, review_run_id: int):
    row = db.query_one("SELECT * FROM review_runs WHERE id = ?", (review_run_id,))
    if not row:
        raise NotFound("찾을 수 없습니다.")
    rr = ReviewRun.from_row(row)
    slug = db.scalar("SELECT slug FROM projects WHERE id = ?", (rr.project_id,))
    project, role = projects.get(actor, slug)
    return rr, project, role


def issues(review_run_id: int):
    return db.query(
        "SELECT * FROM review_issues WHERE review_run_id = ? ORDER BY CASE severity WHEN 'critical' THEN 0"
        " WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, file, line", (review_run_id,))
