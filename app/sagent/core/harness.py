"""Project harness: the `.sagent/` policy files inside a project repository.

`.sagent/` is the source of truth for project policy (shareable via git).
CLAUDE.md / AGENTS.md are generated from it only when they do not exist yet.
Secrets never belong here.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from sagent.core import audit, loopdef, projects, rbac
from sagent.core.errors import Forbidden, ValidationError

CONFIG_DIR = ".sagent"
FILES = ("project.yaml", "harness.yaml", "loops.yaml", "tests.yaml", "review.yaml")
FILE_PERM = {name: "harness.edit" for name in FILES} | {"loops.yaml": "loop.edit"}
SEVERITIES = ("critical", "high", "medium", "low")
BROWSERS = ("chromium", "firefox", "webkit")
MAX_FILE = 128 * 1024
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,99}$")
PERMISSION_MODES = {
    "claude": ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan", "default"),
    "codex": ("read-only", "workspace-write", "danger-full-access"),
}

DEFAULT_RULES = [
    "Keep changes focused on the task; do not refactor unrelated code.",
    "Never commit secrets, credentials or personal data.",
    "Add or update tests for behaviour you change.",
]


def dump(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def _context_files(scan: dict) -> list[str]:
    files = [scan["readme"]] if scan.get("readme") else []
    files += [f for f in ("CLAUDE.md", "AGENTS.md") if scan.get("agent_files", {}).get(f)]
    return files


def build(project, scan: dict, answers: dict) -> dict[str, dict]:
    coding = answers.get("coding_agent") or project.primary_agent
    review = answers.get("review_agent") or "same"
    review = coding if review == "same" else review
    cmds = {**scan.get("commands", {}), **{k: v for k, v in answers.get("commands", {}).items() if v is not None}}
    e2e_enabled = bool(answers.get("e2e", bool(cmds.get("e2e"))))
    loop_name = answers.get("loop", "standard")
    if loop_name not in loopdef.TEMPLATES:
        raise ValidationError("알 수 없는 Loop 템플릿입니다.")
    loops = {name: loopdef.template(name) for name in loopdef.TEMPLATES}
    return {
        "project.yaml": {
            "schema_version": 1,
            "name": project.name,
            "description": project.description,
            "languages": scan.get("languages", []),
            "frameworks": scan.get("frameworks", []),
        },
        "harness.yaml": {
            "context": _context_files(scan),
            "rules": list(answers.get("rules") or DEFAULT_RULES),
            "forbidden": ["Do not push to remote branches.", "Do not edit files outside this repository."],
            "git": {"branch_prefix": "sagent/", "commit_convention": "conventional"},
            "agents": {
                "coding": {"provider": coding},
                "review": {"provider": review},
                "test_analysis": {"provider": coding},
            },
            "permissions": {
                "filesystem": {"project": "write"},
                "shell": {"allow": True},
                "network": {"allow": True},
                "dangerous_commands": {"confirm": True},
            },
            "telemetry": {"otel": False},
        },
        "loops.yaml": {"default": loop_name, "loops": loops},
        "tests.yaml": {
            "unit": {"command": cmds.get("unit", ""), "timeout_sec": 900},
            "lint": {"command": cmds.get("lint", ""), "timeout_sec": 300},
            "typecheck": {"command": cmds.get("typecheck", ""), "timeout_sec": 300},
            "e2e": {
                "enabled": e2e_enabled,
                "framework": "playwright",
                "command": cmds.get("e2e", "") or ("npx playwright test" if e2e_enabled else ""),
                "browsers": [b for b in answers.get("browsers", ["chromium"]) if b in BROWSERS] or ["chromium"],
                "visual_regression": answers.get("visual_regression", "off"),
                "timeout_sec": 1500,
            },
        },
        "review.yaml": {
            "provider": review,
            "inputs": ["diff", "changed_files", "tests", "lint", "typecheck", "rules"],
            "block_on": ["critical", "high"],
            "max_diff_kb": 200,
        },
    }


def validate(name: str, data) -> list[str]:
    if name not in FILES:
        return [f"unknown file {name}"]
    if not isinstance(data, dict):
        return ["top level must be a mapping"]
    errs: list[str] = []
    if name == "harness.yaml":
        for key in ("rules", "forbidden", "context"):
            v = data.get(key, [])
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                errs.append(f"{key}: must be a list of strings")
        agents = data.get("agents", {})
        if not isinstance(agents, dict):
            errs.append("agents: must be a mapping")
        else:
            for role, cfg in agents.items():
                prov = (cfg or {}).get("provider") if isinstance(cfg, dict) else None
                if prov not in projects.AGENTS:
                    errs.append(f"agents.{role}.provider: one of {', '.join(projects.AGENTS)}")
                    continue
                model = cfg.get("model")
                if model is not None and not (isinstance(model, str) and MODEL_RE.match(model)):
                    errs.append(f"agents.{role}.model: invalid model name")
                mode = cfg.get("permission_mode")
                if mode is not None and mode not in PERMISSION_MODES[prov]:
                    errs.append(f"agents.{role}.permission_mode: one of {', '.join(PERMISSION_MODES[prov])}")
    elif name == "tests.yaml":
        for suite in ("unit", "lint", "typecheck", "e2e"):
            s = data.get(suite, {})
            if not isinstance(s, dict):
                errs.append(f"{suite}: must be a mapping")
                continue
            if not isinstance(s.get("command", ""), str):
                errs.append(f"{suite}.command: must be a string")
            t = s.get("timeout_sec", 60)
            if not isinstance(t, int) or not 1 <= t <= 86400:
                errs.append(f"{suite}.timeout_sec: integer 1..86400")
        browsers = (data.get("e2e") or {}).get("browsers", [])
        if not isinstance(browsers, list) or any(b not in BROWSERS for b in browsers):
            errs.append(f"e2e.browsers: subset of {', '.join(BROWSERS)}")
    elif name == "review.yaml":
        if data.get("provider") not in projects.AGENTS:
            errs.append(f"provider: one of {', '.join(projects.AGENTS)}")
        if any(s not in SEVERITIES for s in data.get("block_on", [])):
            errs.append(f"block_on: subset of {', '.join(SEVERITIES)}")
    elif name == "loops.yaml":
        loops = data.get("loops")
        if not isinstance(loops, dict) or not loops:
            errs.append("loops: must be a non-empty mapping")
        else:
            for lname, loop in loops.items():
                errs += loopdef.validate_loop(str(lname), loop)
            if data.get("default") not in loops:
                errs.append("default: must name one of the loops")
    return errs


def parse(name: str, text: str) -> dict:
    if len(text.encode()) > MAX_FILE:
        raise ValidationError("파일이 너무 큽니다.")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValidationError(f"YAML 오류: {exc}") from None
    errs = validate(name, data)
    if errs:
        raise ValidationError(f"{name}: " + "; ".join(errs[:8]))
    return data


# --- filesystem ------------------------------------------------------------

def config_dir(project, create: bool = False) -> Path:
    root = Path(project.path)
    d = root / CONFIG_DIR
    if d.is_symlink():
        raise Forbidden(".sagent 가 심볼릭 링크입니다. 보안을 위해 거부합니다.")
    if create:
        d.mkdir(exist_ok=True)
    return d


def _safe_file(project, name: str, create_dir: bool = False) -> Path:
    if name not in FILES:
        raise ValidationError("알 수 없는 설정 파일입니다.")
    p = config_dir(project, create=create_dir) / name
    if p.is_symlink():
        raise Forbidden(f"{name} 이(가) 심볼릭 링크입니다. 보안을 위해 거부합니다.")
    return p


def _write_text(path: Path, text: str) -> None:
    """Atomic write that never follows a planted symlink (O_EXCL|O_NOFOLLOW)."""
    import secrets as _secrets

    tmp = path.with_name(f".{path.name}.{_secrets.token_hex(6)}.sagent-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_all(project) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name in FILES:
        p = _safe_file(project, name)
        out[name] = p.read_text(encoding="utf-8", errors="replace")[:MAX_FILE] if p.is_file() else None
    return out


def load(project) -> dict[str, dict]:
    """Parsed config, falling back to generated defaults for missing files."""
    defaults = build(project, {"commands": {}}, {})
    out = {}
    for name, text in read_all(project).items():
        data = None
        if text is not None:
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError:
                data = None
        out[name] = data if isinstance(data, dict) else defaults[name]
    return out


def save_file(actor, slug: str, name: str, text: str):
    project, role = projects.get(actor, slug)
    if not rbac.role_allows(role, FILE_PERM.get(name, "harness.edit")):
        raise Forbidden("이 설정을 편집할 권한이 없습니다.")
    parse(name, text)
    path = _safe_file(project, name, create_dir=True)
    _write_text(path, text if text.endswith("\n") else text + "\n")
    audit.record("harness.save", actor, "project", project.id, {"file": name})
    return project


def _agent_doc(project, cfg: dict[str, dict], agent_name: str) -> str:
    h = cfg["harness.yaml"]
    t = cfg["tests.yaml"]
    lines = [
        f"# {project.name} — agent instructions",
        "",
        f"<!-- Generated by sagent from .sagent/ for {agent_name}. Edit .sagent/harness.yaml and regenerate, or edit freely. -->",
        "",
        "## Rules",
        *[f"- {r}" for r in h.get("rules", [])],
        "",
        "## Forbidden",
        *[f"- {r}" for r in h.get("forbidden", [])],
        "",
        "## Commands",
    ]
    for suite in ("unit", "lint", "typecheck"):
        cmd = (t.get(suite) or {}).get("command")
        if cmd:
            lines.append(f"- {suite}: `{cmd}`")
    e2e = t.get("e2e") or {}
    if e2e.get("enabled") and e2e.get("command"):
        lines.append(f"- e2e: `{e2e['command']}`")
    if h.get("context"):
        lines += ["", "## Context files", *[f"- {c}" for c in h["context"]]]
    return "\n".join(lines) + "\n"


def bootstrap(actor, slug: str, scan: dict, answers: dict, overwrite: bool = False) -> dict:
    """Write .sagent/ files (and CLAUDE.md / AGENTS.md if missing)."""
    project, _ = projects.get(actor, slug, "harness.edit")
    cfg = build(project, scan, answers)
    written, skipped = [], []
    config_dir(project, create=True)
    for name, data in cfg.items():
        path = _safe_file(project, name)
        if path.exists() and not overwrite:
            skipped.append(name)
            continue
        _write_text(path, dump(data))
        written.append(name)
    docs = {}
    targets = {"CLAUDE.md": "Claude Code", "AGENTS.md": "Codex"}
    wanted = {cfg["harness.yaml"]["agents"][r]["provider"] for r in ("coding", "review")}
    for fname, label in targets.items():
        if (fname == "CLAUDE.md" and "claude" not in wanted) or (fname == "AGENTS.md" and "codex" not in wanted):
            continue
        path = Path(project.path) / fname
        content = _agent_doc(project, cfg, label)
        if path.exists() or path.is_symlink():
            docs[fname] = {"status": "exists", "suggestion": content}
        else:
            _write_text(path, content)
            docs[fname] = {"status": "created"}
            written.append(fname)
    if project.lifecycle == "BOOTSTRAP":
        projects.update(actor, slug, lifecycle="DEVELOPMENT")
    audit.record("harness.bootstrap", actor, "project", project.id, {"written": written, "skipped": skipped})
    return {"written": written, "skipped": skipped, "docs": docs}
