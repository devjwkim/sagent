"""Environment checks (`sagent doctor`)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = False


def _version(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"error: {exc.__class__.__name__}"
    text = (out.stdout or out.stderr).strip().splitlines()
    return text[0][:120] if text else ""


def _binary(name: str, args: list[str], required: bool = False) -> Check:
    path = shutil.which(name)
    if not path:
        return Check(name, False, "not found in PATH", required)
    return Check(name, True, _version([path, *args]) or path, required)


def run_checks() -> list[Check]:
    py_ok = sys.version_info >= (3, 11)
    checks = [
        Check("python", py_ok, sys.version.split()[0], True),
        _binary("tmux", ["-V"], required=True),
        _binary("git", ["--version"], required=True),
        _binary("claude", ["--version"]),
        _binary("codex", ["--version"]),
        _binary("npx", ["--version"]),
    ]
    try:
        from sagent.core import telemetry

        ep = telemetry.endpoint()
        if not ep:
            checks.append(Check("otel", True, "local only (no OTLP endpoint configured)"))
        elif telemetry.available():
            checks.append(Check("otel", True, f"exporting to {ep}"))
        else:
            checks.append(Check("otel", False, "endpoint set but opentelemetry missing: pip install 'sagent[otel]'"))
    except Exception as exc:  # DB not configured (pure CLI check)
        checks.append(Check("otel", False, f"unknown ({exc.__class__.__name__})"))
    return checks
