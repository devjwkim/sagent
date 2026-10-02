"""Run git safely inside user repositories.

Repository-local config can execute commands (core.fsmonitor, hooks,
external diff/textconv), so every invocation disables those.
"""
from __future__ import annotations

import os
import subprocess

SAFE_CONFIG = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "diff.external=",
               "-c", "core.pager=cat", "-c", "protocol.allow=never"]


def git(path: str, *args: str, timeout: int = 30, text: bool = False):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
           "GIT_CONFIG_NOSYSTEM": "1", "HOME": os.environ.get("HOME", "/")}
    return subprocess.run(["git", *SAFE_CONFIG, "-C", path, *args], capture_output=True, text=text,
                          timeout=timeout, env=env)
