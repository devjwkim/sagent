#!/usr/bin/env python3
"""Pre-publish scanner: fail if tracked files contain secrets or private infra details.

Usage:
    python scripts/check_secrets.py            # scan files tracked by git (or all files)
    python scripts/check_secrets.py PATH ...   # scan given files/dirs

Extra private words (hostnames, handles) can be listed one per line in a file
named by $SAGENT_PRIVATE_WORDS; that file must live outside the repository.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}")),
    ("openai-key", re.compile(r"\bsk-(proj-)?[A-Za-z0-9_-]{20,}")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("telegram-token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("private-ip", re.compile(
        r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
    )),
    ("dyndns-host", re.compile(r"\b[\w-]+\.(duckdns\.org|iptime\.org|ddns\.net|no-ip\.\w+)\b", re.I)),
    ("home-path", re.compile(r"/(home|Users)/(?!user\b|alice\b|you\b|<)[A-Za-z0-9._-]+/")),
    ("workspace-path", re.compile(r"/data/workspace\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@(?!example\.(com|org)\b)(?!users\.noreply\.github\.com\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("assignment", re.compile(
        r"(?i)\b(password|passwd|secret|api_key|apikey|token)\s*[=:]\s*['\"][^'\"\s]{8,}['\"]"
    )),
]

# Lines that are allowed to match (test fixtures, docs about the patterns themselves).
ALLOW_MARK = "check_secrets: allow"
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".pyc"}
SKIP_FILES = {"htmx.min.js"}
SELF = Path(__file__).resolve()


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
        return [ROOT / p for p in out.split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]


def private_words() -> list[str]:
    path = os.environ.get("SAGENT_PRIVATE_WORDS")
    if not path or not Path(path).is_file():
        return []
    return [w.strip() for w in Path(path).read_text().splitlines() if w.strip() and not w.startswith("#")]


def scan(files: list[Path]) -> list[str]:
    words = private_words()
    problems = []
    for f in files:
        if f.resolve() == SELF or f.suffix in SKIP_SUFFIXES or f.name in SKIP_FILES or not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if ALLOW_MARK in line:
                continue
            for name, rx in PATTERNS:
                if rx.search(line):
                    problems.append(f"{f.relative_to(ROOT) if f.is_relative_to(ROOT) else f}:{n}: {name}")
            low = line.lower()
            for w in words:
                if w.lower() in low:
                    problems.append(f"{f.relative_to(ROOT) if f.is_relative_to(ROOT) else f}:{n}: private-word")
    return problems


def main(argv: list[str]) -> int:
    if argv:
        files = []
        for a in argv:
            p = Path(a).resolve()
            files += [x for x in p.rglob("*") if x.is_file()] if p.is_dir() else [p]
    else:
        files = tracked_files()
    problems = scan(files)
    for p in problems:
        print(p)
    print(f"check_secrets: {len(files)} files, {len(problems)} problem(s)", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
