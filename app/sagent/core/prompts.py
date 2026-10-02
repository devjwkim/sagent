"""Versioned prompt templates (LLMOps).

Templates are per project, immutable once saved: editing creates version N+1.
Loop agent nodes reference them with `prompt_ref: name` (latest) or
`name@3` (pinned); each run records the template and version it used, so
success rate / tokens / cost can be compared between versions.
"""
from __future__ import annotations

import re

from sagent import db
from sagent.core import audit, projects
from sagent.core.errors import NotFound, ValidationError

db.register_schema("""
CREATE TABLE IF NOT EXISTS prompt_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_by INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, name, version)
);
""")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
REF_RE = re.compile(r"^([a-z0-9][a-z0-9_-]{0,47})(?:@(\d{1,6}))?$")
MAX_BODY = 50_000
PLACEHOLDERS = ("{task}", "{plan}", "{failure}", "{review}")


def save(actor, slug: str, name: str, body: str, note: str = "") -> dict:
    project, _ = projects.get(actor, slug, "prompt.edit")
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValidationError("템플릿 이름은 영문 소문자·숫자·_- 로 1~48자여야 합니다.")
    body = (body or "").replace("\r\n", "\n").strip()
    if not body:
        raise ValidationError("본문을 입력하세요.")
    if len(body) > MAX_BODY:
        raise ValidationError("본문이 너무 깁니다.")
    with db.connect() as conn:
        last = conn.execute(
            "SELECT version, body FROM prompt_templates WHERE project_id = ? AND name = ?"
            " ORDER BY version DESC LIMIT 1", (project.id, name)).fetchone()
        if last and last["body"] == body:
            raise ValidationError("이전 버전과 내용이 같습니다.")
        version = (last["version"] if last else 0) + 1
        conn.execute(
            "INSERT INTO prompt_templates (project_id, name, version, body, note, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project.id, name, version, body, (note or "")[:300], actor.id, db.utcnow()))
    audit.record("prompt.save", actor, "project", project.id, {"name": name, "version": version})
    return {"name": name, "version": version}


def list_latest(actor, slug: str):
    project, _ = projects.get(actor, slug)
    return db.query(
        "SELECT t.*, (SELECT COUNT(*) FROM prompt_templates x WHERE x.project_id = t.project_id"
        " AND x.name = t.name) AS versions FROM prompt_templates t WHERE t.project_id = ? AND t.version ="
        " (SELECT MAX(version) FROM prompt_templates y WHERE y.project_id = t.project_id AND y.name = t.name)"
        " ORDER BY t.name", (project.id,))


def versions(actor, slug: str, name: str):
    project, _ = projects.get(actor, slug)
    rows = db.query("SELECT * FROM prompt_templates WHERE project_id = ? AND name = ? ORDER BY version DESC",
                    (project.id, name))
    if not rows:
        raise NotFound("템플릿을 찾을 수 없습니다.")
    return rows


def resolve(project_id: int, ref: str) -> tuple[str, int, str]:
    m = REF_RE.match(ref or "")
    if not m:
        raise ValidationError(f"잘못된 prompt_ref: {ref}")
    name, ver = m.group(1), m.group(2)
    if ver:
        row = db.query_one("SELECT * FROM prompt_templates WHERE project_id = ? AND name = ? AND version = ?",
                           (project_id, name, int(ver)))
    else:
        row = db.query_one("SELECT * FROM prompt_templates WHERE project_id = ? AND name = ?"
                           " ORDER BY version DESC LIMIT 1", (project_id, name))
    if not row:
        raise ValidationError(f"프롬프트 템플릿 '{ref}' 이(가) 없습니다.")
    return row["name"], row["version"], row["body"]


def stats(actor, slug: str, name: str):
    """Outcome metrics per version, from usage records of runs that used it."""
    project, _ = projects.get(actor, slug)
    return db.query(
        "SELECT prompt_version AS version, COUNT(*) AS runs,"
        " SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) AS ok,"
        " AVG(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens) AS avg_tokens,"
        " AVG(cost_usd) AS avg_cost, AVG(duration_ms) AS avg_ms"
        " FROM usage_records WHERE project_id = ? AND prompt_template = ? GROUP BY prompt_version"
        " ORDER BY prompt_version DESC", (project.id, name))
