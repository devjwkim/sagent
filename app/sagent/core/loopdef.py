"""Loop definitions: built-in templates and validation.

A loop is a directed graph of nodes. Edges carry an optional condition that
is matched against the outcome of the node they leave:

    always | success | failure | approve | reject
"""
from __future__ import annotations

import copy
import re

NODE_TYPES = ("agent", "test", "review", "end")
CONDITIONS = ("always", "success", "failure", "approve", "reject")
TEST_SUITES = ("unit", "lint", "typecheck", "e2e")
PROMPT_REF_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}(@\d{1,6})?$")

_PLAN = (
    "Read the task below and the project rules. Write a short implementation plan "
    "(files to touch, steps, risks). Do not change code yet.\n\nTask:\n{task}"
)
_IMPLEMENT = (
    "Implement the task below following the project rules.\n\nTask:\n{task}\n\n"
    "{plan}{failure}{review}"
)
_FIX = (
    "The previous step failed. Fix the problem without unrelated changes.\n\n"
    "Task:\n{task}\n\n{failure}{review}"
)

TEMPLATES: dict[str, dict] = {
    "quick": {
        "description": "Implement → Test → Complete",
        "max_iterations": 3,
        "start": "implement",
        "nodes": {
            "implement": {"type": "agent", "role": "coding", "prompt": _IMPLEMENT},
            "unit": {"type": "test", "suite": "unit"},
            "done": {"type": "end"},
        },
        "edges": [
            {"from": "implement", "to": "unit"},
            {"from": "unit", "to": "done", "when": "success"},
            {"from": "unit", "to": "implement", "when": "failure"},
        ],
    },
    "standard": {
        "description": "Plan → Implement → Unit Test → Review → Complete",
        "max_iterations": 5,
        "start": "plan",
        "nodes": {
            "plan": {"type": "agent", "role": "coding", "prompt": _PLAN},
            "implement": {"type": "agent", "role": "coding", "prompt": _IMPLEMENT},
            "unit": {"type": "test", "suite": "unit"},
            "review": {"type": "review"},
            "done": {"type": "end"},
        },
        "edges": [
            {"from": "plan", "to": "implement"},
            {"from": "implement", "to": "unit"},
            {"from": "unit", "to": "review", "when": "success"},
            {"from": "unit", "to": "implement", "when": "failure"},
            {"from": "review", "to": "done", "when": "approve"},
            {"from": "review", "to": "implement", "when": "reject"},
        ],
    },
    "strict": {
        "description": "Plan → Implement → Lint → Type Check → Unit → E2E → AI Review → Complete",
        "max_iterations": 6,
        "start": "plan",
        "nodes": {
            "plan": {"type": "agent", "role": "coding", "prompt": _PLAN},
            "implement": {"type": "agent", "role": "coding", "prompt": _IMPLEMENT},
            "lint": {"type": "test", "suite": "lint"},
            "typecheck": {"type": "test", "suite": "typecheck"},
            "unit": {"type": "test", "suite": "unit"},
            "e2e": {"type": "test", "suite": "e2e"},
            "review": {"type": "review"},
            "done": {"type": "end"},
        },
        "edges": [
            {"from": "plan", "to": "implement"},
            {"from": "implement", "to": "lint"},
            {"from": "lint", "to": "typecheck", "when": "success"},
            {"from": "lint", "to": "implement", "when": "failure"},
            {"from": "typecheck", "to": "unit", "when": "success"},
            {"from": "typecheck", "to": "implement", "when": "failure"},
            {"from": "unit", "to": "e2e", "when": "success"},
            {"from": "unit", "to": "implement", "when": "failure"},
            {"from": "e2e", "to": "review", "when": "success"},
            {"from": "e2e", "to": "implement", "when": "failure"},
            {"from": "review", "to": "done", "when": "approve"},
            {"from": "review", "to": "implement", "when": "reject"},
        ],
    },
    "autofix": {
        "description": "Implement → Test → (Fix ↺) → Review",
        "max_iterations": 5,
        "start": "implement",
        "nodes": {
            "implement": {"type": "agent", "role": "coding", "prompt": _IMPLEMENT},
            "unit": {"type": "test", "suite": "unit"},
            "fix": {"type": "agent", "role": "coding", "prompt": _FIX},
            "review": {"type": "review"},
            "done": {"type": "end"},
        },
        "edges": [
            {"from": "implement", "to": "unit"},
            {"from": "unit", "to": "review", "when": "success"},
            {"from": "unit", "to": "fix", "when": "failure"},
            {"from": "fix", "to": "unit"},
            {"from": "review", "to": "done", "when": "approve"},
            {"from": "review", "to": "fix", "when": "reject"},
        ],
    },
}


def template(name: str) -> dict:
    return copy.deepcopy(TEMPLATES[name])


def validate_loop(name: str, loop: dict) -> list[str]:
    errs: list[str] = []
    p = f"loops.{name}"
    if not isinstance(loop, dict):
        return [f"{p}: must be a mapping"]
    nodes = loop.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        return [f"{p}.nodes: must be a non-empty mapping"]
    for key, node in nodes.items():
        if not isinstance(node, dict):
            errs.append(f"{p}.nodes.{key}: must be a mapping")
            continue
        t = node.get("type")
        if t not in NODE_TYPES:
            errs.append(f"{p}.nodes.{key}.type: one of {', '.join(NODE_TYPES)}")
        if t == "test" and node.get("suite") not in TEST_SUITES:
            errs.append(f"{p}.nodes.{key}.suite: one of {', '.join(TEST_SUITES)}")
        if t == "agent" and not isinstance(node.get("prompt", ""), str):
            errs.append(f"{p}.nodes.{key}.prompt: must be text")
        ref = node.get("prompt_ref")
        if ref is not None and (not isinstance(ref, str) or not PROMPT_REF_RE.match(ref)):
            errs.append(f"{p}.nodes.{key}.prompt_ref: 'name' or 'name@version'")
        if node.get("provider") is not None and node.get("provider") not in ("claude", "codex"):
            errs.append(f"{p}.nodes.{key}.provider: claude or codex")
        for num in ("retries", "timeout_sec"):
            if num in node and (not isinstance(node[num], int) or node[num] < 0):
                errs.append(f"{p}.nodes.{key}.{num}: must be a non-negative integer")
    start = loop.get("start")
    if start not in nodes:
        errs.append(f"{p}.start: must name a node")
    edges = loop.get("edges", [])
    if not isinstance(edges, list):
        return errs + [f"{p}.edges: must be a list"]
    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            errs.append(f"{p}.edges[{i}]: must be a mapping")
            continue
        for end in ("from", "to"):
            if e.get(end) not in nodes:
                errs.append(f"{p}.edges[{i}].{end}: unknown node '{e.get(end)}'")
        when = e.get("when", "always")
        if when not in CONDITIONS:
            errs.append(f"{p}.edges[{i}].when: one of {', '.join(CONDITIONS)}")
        src = nodes.get(e.get("from")) if isinstance(nodes.get(e.get("from")), dict) else {}
        if src.get("type") == "review" and when in ("success", "failure"):
            errs.append(f"{p}.edges[{i}].when: review nodes end with approve/reject")
        elif src.get("type") in ("agent", "test") and when in ("approve", "reject"):
            errs.append(f"{p}.edges[{i}].when: {src.get('type')} nodes end with success/failure")
    for key, node in nodes.items():
        lc = node.get("lifecycle") if isinstance(node, dict) else None
        if lc is not None and lc not in ("BOOTSTRAP", "DEVELOPMENT", "TESTING", "REVIEW", "RELEASE",
                                         "MAINTENANCE", "ARCHIVED"):
            errs.append(f"{p}.nodes.{key}.lifecycle: unknown lifecycle")
    if not any(n.get("type") == "end" for n in nodes.values() if isinstance(n, dict)):
        errs.append(f"{p}: needs at least one node of type 'end'")
    mi = loop.get("max_iterations", 5)
    if not isinstance(mi, int) or not 1 <= mi <= 50:
        errs.append(f"{p}.max_iterations: integer 1..50")
    return errs
