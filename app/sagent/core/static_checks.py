"""Deterministic checks on a diff, run before/alongside the AI reviewer.

They do not depend on the model, so prompt injection inside the change
("AI reviewers must approve…") cannot talk them out of a finding.
"""
from __future__ import annotations

import re

SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("API key (sk-…)", re.compile(r"\bsk-(live|proj|test)?-?[A-Za-z0-9_-]{24,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Stripe key", re.compile(r"\b(sk|rk)_(live|test)_[A-Za-z0-9]{16,}")),
    ("hardcoded credential", re.compile(
        r"(?i)\b[\w]*(password|passwd|secret|api_?key|access_?token|auth_?token|token)\w*\s*[:=]\s*['\"][^'\"\s]{12,}['\"]")),
]
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def scan_diff(diff: str, extra_files: list[tuple[str, str]] | None = None, limit: int = 50) -> list[dict]:
    """Return review issues for secrets in added lines (and new untracked files)."""
    issues: list[dict] = []
    seen: set[tuple[str, int, str]] = set()

    def add(path: str, line: int | None, label: str) -> None:
        key = (path, line or 0, label)
        if key in seen or len(issues) >= limit:
            return
        seen.add(key)
        issues.append({
            "severity": "critical", "file": path[:300], "line": line, "category": "secret",
            "reason": f"Static check: possible {label} added to the repository.",
            "suggested_fix": "Remove it from the code, rotate it, and load it from the environment or a secret store.",
            "confidence": 0.9,
        })

    path, lineno = "", None
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else raw[4:]
            continue
        m = _HUNK.match(raw)
        if m:
            lineno = int(m.group(1))
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            for label, rx in SECRET_PATTERNS:
                if rx.search(raw):
                    add(path, lineno, label)
                    break
            lineno = lineno + 1 if lineno is not None else None
        elif raw.startswith(" ") and lineno is not None:
            lineno += 1
    for rel, text in extra_files or []:
        for n, line in enumerate(text.splitlines(), 1):
            for label, rx in SECRET_PATTERNS:
                if rx.search(line):
                    add(rel, n, label)
                    break
    return issues
