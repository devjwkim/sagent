"""Organisation-wide loop template library.

Built-in templates (quick / standard / strict / autofix) plus loops users
share from their projects. Adding a template copies it into the project's
`.sagent/loops.yaml` (the project stays the source of truth).
"""
from __future__ import annotations

import json

from sagent import db
from sagent.core import audit, loop_editor, loopdef, loops, projects, rbac
from sagent.core.errors import Conflict, Forbidden, NotFound, ValidationError

db.register_schema("""
CREATE TABLE IF NOT EXISTS loop_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    spec TEXT NOT NULL,
    source_project_id INTEGER,
    created_by INTEGER,
    created_at TEXT NOT NULL
);
""")


def list_all() -> list[dict]:
    items = [{"id": None, "name": n, "description": t.get("description", ""), "spec": t, "builtin": True,
              "created_by": None} for n, t in loopdef.TEMPLATES.items()]
    for r in db.query("SELECT * FROM loop_templates ORDER BY name"):
        items.append({"id": r["id"], "name": r["name"], "description": r["description"],
                      "spec": json.loads(r["spec"]), "builtin": False, "created_by": r["created_by"]})
    return items


def share(actor, slug: str, loop_name: str, template_name: str, description: str = "") -> int:
    project, role = projects.get(actor, slug)
    if not rbac.role_allows(role, "loop.edit"):
        raise Forbidden("Loop 를 공유할 권한이 없습니다.")
    defs, _ = loops.definitions(project)
    spec = defs.get(loop_name)
    if spec is None:
        raise NotFound("Loop 를 찾을 수 없습니다.")
    template_name = (template_name or loop_name).strip().lower()
    if not loop_editor.NAME_RE.match(template_name) or template_name in loopdef.TEMPLATES:
        raise ValidationError("템플릿 이름이 올바르지 않거나 기본 템플릿과 겹칩니다.")
    errs = loopdef.validate_loop(template_name, spec)
    if errs:
        raise ValidationError("; ".join(errs[:5]))
    if db.query_one("SELECT 1 FROM loop_templates WHERE name = ?", (template_name,)):
        raise Conflict("같은 이름의 템플릿이 이미 있습니다.")
    tid = db.execute(
        "INSERT INTO loop_templates (name, description, spec, source_project_id, created_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (template_name, (description or spec.get("description") or "")[:200], json.dumps(spec), project.id,
         actor.id, db.utcnow()))
    audit.record("loop_template.share", actor, "loop_template", tid, {"name": template_name})
    return tid


def add_to_project(actor, slug: str, template_name: str, as_name: str = "") -> str:
    item = next((t for t in list_all() if t["name"] == template_name), None)
    if item is None:
        raise NotFound("템플릿을 찾을 수 없습니다.")
    name = (as_name or template_name).strip().lower()
    loop_editor.save(actor, slug, name, item["spec"])
    return name


def delete(actor, template_id: int) -> None:
    row = db.query_one("SELECT * FROM loop_templates WHERE id = ?", (template_id,))
    if not row:
        raise NotFound("템플릿을 찾을 수 없습니다.")
    if not actor.is_admin and row["created_by"] != actor.id:
        raise Forbidden("만든 사람 또는 관리자만 삭제할 수 있습니다.")
    db.execute("DELETE FROM loop_templates WHERE id = ?", (template_id,))
    audit.record("loop_template.delete", actor, "loop_template", template_id, {"name": row["name"]})

