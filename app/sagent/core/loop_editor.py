"""Save / delete loop definitions edited in the visual editor.

The editor sends one loop as JSON; it is validated with the same rules as the
YAML file and written back into `.sagent/loops.yaml` (comments in that file
are not preserved). Node positions live in an optional `ui: {x, y}` key.
"""
from __future__ import annotations

import re

from sagent.core import harness, loopdef, projects, rbac
from sagent.core.errors import Forbidden, ValidationError

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
NODE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
NODE_FIELDS = {
    "agent": ("type", "role", "provider", "prompt", "prompt_ref", "retries", "timeout_sec", "ui"),
    "test": ("type", "suite", "retries", "timeout_sec", "ui"),
    "review": ("type", "provider", "retries", "timeout_sec", "ui"),
    "end": ("type", "lifecycle", "ui"),
}


def _clean(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise ValidationError("잘못된 Loop 데이터입니다.")
    nodes_in = spec.get("nodes")
    if not isinstance(nodes_in, dict) or not nodes_in or len(nodes_in) > 50:
        raise ValidationError("노드는 1~50개여야 합니다.")
    nodes = {}
    for key, node in nodes_in.items():
        if not isinstance(key, str) or not NODE_KEY_RE.match(key):
            raise ValidationError(f"노드 이름 '{key}' 은(는) 영문 소문자·숫자·_- 로 1~40자여야 합니다.")
        if not isinstance(node, dict) or node.get("type") not in NODE_FIELDS:
            raise ValidationError(f"노드 '{key}' 의 type 이 올바르지 않습니다.")
        clean = {}
        for f in NODE_FIELDS[node["type"]]:
            v = node.get(f)
            if v in (None, "") or (f == "role" and v == "coding"):
                continue
            if f in ("retries", "timeout_sec"):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise ValidationError(f"{key}.{f} 는 정수여야 합니다.") from None
                if v == 0:
                    continue
            if f == "ui":
                if not isinstance(v, dict):
                    continue
                try:
                    v = {"x": max(0, min(5000, int(v.get("x", 0)))), "y": max(0, min(5000, int(v.get("y", 0))))}
                except (TypeError, ValueError):
                    continue
            if f in ("prompt",) and len(str(v)) > 20000:
                raise ValidationError(f"{key}.prompt 가 너무 깁니다.")
            clean[f] = v
        nodes[key] = clean
    edges = []
    for e in spec.get("edges") or []:
        if not isinstance(e, dict):
            continue
        edge = {"from": e.get("from"), "to": e.get("to")}
        if e.get("when") and e["when"] != "always":
            edge["when"] = e["when"]
        edges.append(edge)
    out = {}
    if spec.get("description"):
        out["description"] = str(spec["description"])[:200]
    try:
        out["max_iterations"] = int(spec.get("max_iterations") or 5)
    except (TypeError, ValueError):
        raise ValidationError("max_iterations 는 정수여야 합니다.") from None
    out["start"] = spec.get("start")
    out["nodes"] = nodes
    out["edges"] = edges
    return out


def save(actor, slug: str, name: str, spec: dict, old_name: str | None = None, make_default: bool = False) -> dict:
    project, role = projects.get(actor, slug)
    if not rbac.role_allows(role, "loop.edit"):
        raise Forbidden("Loop 를 편집할 권한이 없습니다.")
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise ValidationError("Loop 이름은 영문 소문자·숫자·_- 로 1~40자여야 합니다.")
    loop = _clean(spec)
    errs = loopdef.validate_loop(name, loop)
    if errs:
        raise ValidationError("; ".join(errs[:6]))
    data = harness.load(project)["loops.yaml"]
    loops = dict(data.get("loops") or {})
    if old_name and old_name != name:
        loops.pop(old_name, None)
        if data.get("default") == old_name:
            data["default"] = name
    elif not old_name and name in loops:
        raise ValidationError("같은 이름의 Loop 가 이미 있습니다.")
    loops[name] = loop
    data["loops"] = loops
    if make_default or data.get("default") not in loops:
        data["default"] = name
    harness.save_file(actor, slug, "loops.yaml", harness.dump(data))
    return loop


def delete(actor, slug: str, name: str) -> None:
    project, role = projects.get(actor, slug)
    if not rbac.role_allows(role, "loop.edit"):
        raise Forbidden("Loop 를 편집할 권한이 없습니다.")
    data = harness.load(project)["loops.yaml"]
    loops = dict(data.get("loops") or {})
    if name not in loops:
        raise ValidationError("없는 Loop 입니다.")
    if len(loops) == 1:
        raise ValidationError("마지막 Loop 는 삭제할 수 없습니다.")
    loops.pop(name)
    data["loops"] = loops
    if data.get("default") == name:
        data["default"] = next(iter(loops))
    harness.save_file(actor, slug, "loops.yaml", harness.dump(data))
