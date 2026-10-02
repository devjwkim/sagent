"""Bootstrap scanner: inspect an existing project directory (read-only).

Only looks at well-known files near the project root; never walks the whole
tree and never follows symlinks out of the project.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

MAX_READ = 256 * 1024

JS_FRAMEWORKS = [
    ("next", "Next.js"), ("nuxt", "Nuxt"), ("@sveltejs/kit", "SvelteKit"), ("svelte", "Svelte"),
    ("@angular/core", "Angular"), ("vue", "Vue"), ("react", "React"), ("@nestjs/core", "NestJS"),
    ("express", "Express"), ("fastify", "Fastify"), ("astro", "Astro"), ("vite", "Vite"),
]
PY_FRAMEWORKS = [
    ("django", "Django"), ("fastapi", "FastAPI"), ("flask", "Flask"), ("starlette", "Starlette"),
    ("streamlit", "Streamlit"),
]
JS_TESTS = [("vitest", "Vitest"), ("jest", "Jest"), ("mocha", "Mocha"), ("ava", "AVA")]


def _inside(root: Path, p: Path) -> bool:
    try:
        Path(os.path.realpath(p)).relative_to(root)
        return True
    except ValueError:
        return False


def _read(root: Path, rel: str) -> str | None:
    p = root / rel
    if not p.is_file() or not _inside(root, p):
        return None
    try:
        with open(p, "rb") as fh:
            return fh.read(MAX_READ).decode("utf-8", errors="replace")
    except OSError:
        return None


def _exists(root: Path, rel: str) -> bool:
    p = root / rel
    return p.exists() and _inside(root, p)


def _glob_any(root: Path, pattern: str) -> bool:
    return any(_inside(root, p) for p in root.glob(pattern))


def _git(root: Path) -> dict:
    info = {"detected": _exists(root, ".git"), "branch": None, "remote": False, "dirty": None}
    if not info["detected"] or not shutil.which("git"):
        return info

    def run(*args):
        try:
            from sagent.core import gitutil

            out = gitutil.git(str(root), *args, timeout=10, text=True)
            return out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    info["branch"] = run("rev-parse", "--abbrev-ref", "HEAD")
    info["remote"] = bool(run("remote"))
    status = run("status", "--porcelain")
    info["dirty"] = None if status is None else bool(status)
    return info


def _py_deps(root: Path) -> set[str]:
    deps: set[str] = set()
    req = _read(root, "requirements.txt") or ""
    for line in req.splitlines():
        m = re.match(r"\s*([A-Za-z0-9_.-]+)", line)
        if m and not line.strip().startswith("#"):
            deps.add(m.group(1).lower())
    py = _read(root, "pyproject.toml")
    if py:
        try:
            data = tomllib.loads(py)
        except tomllib.TOMLDecodeError:
            data = {}
        proj = data.get("project", {})
        specs = list(proj.get("dependencies", []))
        for extra in proj.get("optional-dependencies", {}).values():
            specs += extra
        for group in data.get("dependency-groups", {}).values():
            specs += [s for s in group if isinstance(s, str)]
        poetry = data.get("tool", {}).get("poetry", {})
        specs += list(poetry.get("dependencies", {}).keys())
        specs += list(poetry.get("group", {}).get("dev", {}).get("dependencies", {}).keys())
        for s in specs:
            m = re.match(r"\s*([A-Za-z0-9_.-]+)", s)
            if m:
                deps.add(m.group(1).lower())
        for tool in data.get("tool", {}):
            deps.add(f"tool:{tool}")
    return deps


def scan(path: str | Path) -> dict:
    root = Path(os.path.realpath(path))
    result: dict = {
        "path": str(root),
        "languages": [],
        "frameworks": [],
        "package_manager": None,
        "unit_test": None,
        "e2e": None,
        "ci": [],
        "agent_files": {},
        "readme": None,
        "sagent_config": _exists(root, ".sagent"),
        "commands": {"unit": "", "lint": "", "typecheck": "", "e2e": ""},
    }
    result["git"] = _git(root)

    # --- JavaScript / TypeScript
    pkg_text = _read(root, "package.json")
    if pkg_text:
        try:
            pkg = json.loads(pkg_text)
        except json.JSONDecodeError:
            pkg = {}
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        scripts = pkg.get("scripts", {}) or {}
        ts = _exists(root, "tsconfig.json") or "typescript" in deps
        result["languages"].append("TypeScript" if ts else "JavaScript")
        result["frameworks"] += [label for key, label in JS_FRAMEWORKS if key in deps]
        if _exists(root, "pnpm-lock.yaml"):
            pm = "pnpm"
        elif _exists(root, "yarn.lock"):
            pm = "yarn"
        elif _exists(root, "bun.lockb") or _exists(root, "bun.lock"):
            pm = "bun"
        else:
            pm = "npm"
        result["package_manager"] = pm
        run = "npm run" if pm == "npm" else pm
        for key, label in JS_TESTS:
            if key in deps:
                result["unit_test"] = label
                break
        if "test" in scripts:
            result["commands"]["unit"] = f"{pm} test"
        if "lint" in scripts:
            result["commands"]["lint"] = f"{run} lint"
        if "typecheck" in scripts:
            result["commands"]["typecheck"] = f"{run} typecheck"
        elif ts:
            result["commands"]["typecheck"] = "npx tsc --noEmit"
        if "@playwright/test" in deps or _glob_any(root, "playwright.config.*"):
            result["e2e"] = "Playwright"
            result["commands"]["e2e"] = "npx playwright test"
        elif "cypress" in deps:
            result["e2e"] = "Cypress"

    # --- Python
    py_deps = _py_deps(root)
    if py_deps or _exists(root, "setup.py") or _exists(root, "pyproject.toml"):
        result["languages"].append("Python")
        result["frameworks"] += [label for key, label in PY_FRAMEWORKS if key in py_deps]
        if (
            "pytest" in py_deps or "tool:pytest" in py_deps or _exists(root, "pytest.ini")
            or _exists(root, "conftest.py") or _exists(root, "tests/conftest.py")
        ):
            result["unit_test"] = result["unit_test"] or "pytest"
            result["commands"]["unit"] = result["commands"]["unit"] or "pytest -q"
        if "ruff" in py_deps or "tool:ruff" in py_deps or _exists(root, "ruff.toml"):
            result["commands"]["lint"] = result["commands"]["lint"] or "ruff check ."
        if "mypy" in py_deps or "tool:mypy" in py_deps or _exists(root, "mypy.ini"):
            result["commands"]["typecheck"] = result["commands"]["typecheck"] or "mypy ."
        if "pytest-playwright" in py_deps and not result["e2e"]:
            result["e2e"] = "Playwright (pytest)"
            result["commands"]["e2e"] = "pytest -q tests/e2e"

    # --- other ecosystems
    if _exists(root, "Cargo.toml"):
        result["languages"].append("Rust")
        result["unit_test"] = result["unit_test"] or "cargo test"
        result["commands"]["unit"] = result["commands"]["unit"] or "cargo test"
        result["commands"]["lint"] = result["commands"]["lint"] or "cargo clippy"
    if _exists(root, "go.mod"):
        result["languages"].append("Go")
        result["commands"]["unit"] = result["commands"]["unit"] or "go test ./..."
        result["commands"]["lint"] = result["commands"]["lint"] or "go vet ./..."
    if _exists(root, "pom.xml"):
        result["languages"].append("Java")
        result["commands"]["unit"] = result["commands"]["unit"] or "mvn -q test"
    if _exists(root, "build.gradle") or _exists(root, "build.gradle.kts"):
        result["languages"].append("Java/Kotlin")
        result["commands"]["unit"] = result["commands"]["unit"] or "./gradlew test"

    # --- CI
    if _glob_any(root, ".github/workflows/*.y*ml"):
        result["ci"].append("GitHub Actions")
    if _exists(root, ".gitlab-ci.yml"):
        result["ci"].append("GitLab CI")
    if _exists(root, "Jenkinsfile"):
        result["ci"].append("Jenkins")
    if _exists(root, ".circleci"):
        result["ci"].append("CircleCI")

    # --- agent files
    for rel in ("CLAUDE.md", "AGENTS.md", ".claude", ".codex"):
        result["agent_files"][rel] = _exists(root, rel)
    for rel in ("README.md", "README.rst", "README.txt", "README"):
        if _exists(root, rel):
            result["readme"] = rel
            break

    result["languages"] = list(dict.fromkeys(result["languages"]))
    result["frameworks"] = list(dict.fromkeys(result["frameworks"]))
    result["agents"] = {
        "claude": bool(shutil.which("claude")),
        "codex": bool(shutil.which("codex")),
    }
    return result
