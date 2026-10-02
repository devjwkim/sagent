"""E2E test scenario wizard.

Detect the pages/routes of a project (read-only, bounded scan), let the user
pick scenarios, then ask the coding agent to write Playwright tests for them.
sagent does not generate tests itself — it prepares the context and the
instruction (PRD §32-33).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from sagent.core import harness, projects, rbac, runs
from sagent.core.errors import ValidationError

MAX_FILES = 2000
MAX_FILE_BYTES = 200 * 1024
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "dist", "build", ".next", "__pycache__", ".sagent",
             "coverage", "test-results", "playwright-report"}
PY_ROUTE = re.compile(r"""@\w+\.(?:route|get|post|put|patch|delete)\(\s*["'](/[^"']*)["']""")
DJANGO_ROUTE = re.compile(r"""\bpath\(\s*["']([^"']*)["']""")
EXPRESS_ROUTE = re.compile(r"""\b(?:app|router)\.(?:get|all)\(\s*["'](/[^"']*)["']""")
DEFAULT_SCENARIOS = ["Login", "Signup", "Logout", "Navigation smoke test", "Form validation errors",
                     "Checkout", "Settings", "Admin area"]


def _walk(root: Path):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            count += 1
            if count > MAX_FILES:
                return
            yield Path(dirpath) / f


def _route_from_segments(parts: list[str]) -> str:
    segs = [p for p in parts if not (p.startswith("(") and p.endswith(")")) and not p.startswith("@")]
    route = "/" + "/".join(segs)
    return re.sub(r"/index$", "", route) or "/"


def detect_pages(path: str) -> list[str]:
    root = Path(os.path.realpath(path))
    routes: set[str] = set()
    for f in _walk(root):
        try:
            rel = f.relative_to(root)
        except ValueError:
            continue
        parts = list(rel.parts)
        name = f.name
        # Next.js app router / pages router, SvelteKit, Nuxt
        if "app" in parts[:2] and re.fullmatch(r"page\.(tsx|jsx|ts|js|mdx)", name):
            i = parts.index("app")
            routes.add(_route_from_segments(parts[i + 1:-1]))
        elif parts[0] in ("pages", "src") and "pages" in parts[:2] and f.suffix in (".tsx", ".jsx", ".js", ".ts", ".vue"):
            i = parts.index("pages")
            segs = parts[i + 1:-1] + [f.stem]
            if segs and segs[0] == "api" or f.stem.startswith("_"):
                continue
            routes.add(_route_from_segments(segs))
        elif name == "+page.svelte" and "routes" in parts:
            i = parts.index("routes")
            routes.add(_route_from_segments(parts[i + 1:-1]))
        elif f.suffix in (".py", ".js", ".ts") and not f.is_symlink():
            try:
                if f.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if f.suffix == ".py":
                routes.update(m for m in PY_ROUTE.findall(text))
                if name == "urls.py":
                    routes.update("/" + m.lstrip("/") for m in DJANGO_ROUTE.findall(text))
            else:
                routes.update(m for m in EXPRESS_ROUTE.findall(text))
    clean = sorted(r for r in routes if len(r) < 120 and "\n" not in r)
    return clean[:100]


def build_prompt(project, pages: list[str], scenarios: list[str], *, browsers: list[str], mobile: bool,
                 visual: str, base_url: str) -> str:
    cfg = harness.load(project)["tests.yaml"].get("e2e") or {}
    lines = [
        "Create Playwright end-to-end tests for this project (use @playwright/test with TypeScript unless the "
        "project already uses pytest-playwright — then follow the existing convention).",
        "",
        "Requirements:",
        "- Put the tests in the project's existing e2e directory, or `tests/e2e/` if there is none.",
        "- Create or update `playwright.config` with the projects listed below; do not remove existing settings.",
        f"- Browsers: {', '.join(browsers) or 'chromium'}" + ("; add a mobile viewport project (e.g. Pixel 7)." if mobile else "."),
        "- Use stable selectors (getByRole / getByLabel / data-testid). No fixed sleeps.",
        "- Keep `trace: 'retain-on-failure'` and `screenshot: 'only-on-failure'`.",
        "- Never hard-code real credentials: read them from environment variables (E2E_USER, E2E_PASSWORD).",
        "- Do not change application code. Only add or edit test files, test fixtures and the Playwright config.",
        "- When done, run the new tests once and fix the tests (not the app) until they pass or the failure is a real bug;",
        "  report real bugs you found in your final message.",
    ]
    if base_url:
        lines.append(f"- Base URL: {base_url} (set it as `use.baseURL`).")
    if visual and visual != "off":
        target = "the most important pages" if visual == "critical" else "every page under test"
        lines.append(f"- Visual regression: add `expect(page).toHaveScreenshot()` checks for {target}.")
    if pages:
        lines += ["", "Detected pages / routes:", *[f"- {p}" for p in pages[:60]]]
    lines += ["", "Scenarios to cover:", *[f"- {s}" for s in scenarios]]
    if cfg.get("command"):
        lines += ["", f"The project's E2E command is `{cfg['command']}`."]
    return "\n".join(lines)


def start(actor, slug: str, *, pages: list[str], scenarios: list[str], browsers: list[str], mobile: bool,
          visual: str = "off", base_url: str = ""):
    project, role = projects.get(actor, slug, "run.start")
    scenarios = [s.strip()[:200] for s in scenarios if s and s.strip()][:40]
    if not scenarios:
        raise ValidationError("시나리오를 하나 이상 선택하세요.")
    if base_url and not re.match(r"^https?://[\w.\-:\[\]]+(/[\w./\-]*)?$", base_url):
        raise ValidationError("Base URL 형식이 올바르지 않습니다.")
    browsers = [b for b in browsers if b in harness.BROWSERS] or ["chromium"]
    if visual not in ("off", "critical", "all"):
        visual = "off"
    # enable e2e in tests.yaml when the user may edit the harness
    if rbac.role_allows(role, "harness.edit"):
        cfg = harness.load(project)
        e2e = dict(cfg["tests.yaml"].get("e2e") or {})
        if not e2e.get("enabled") or not e2e.get("command") or e2e.get("browsers") != browsers \
                or e2e.get("visual_regression") != visual:
            e2e.update(enabled=True, command=e2e.get("command") or "npx playwright test",
                       browsers=browsers, visual_regression=visual)
            data = dict(cfg["tests.yaml"])
            data["e2e"] = e2e
            harness.save_file(actor, slug, "tests.yaml", harness.dump(data))
    prompt = build_prompt(project, pages, scenarios, browsers=browsers, mobile=mobile, visual=visual,
                          base_url=base_url)
    return runs.start_agent(actor, slug, prompt, title=f"E2E 시나리오 생성 ({len(scenarios)}개)")
