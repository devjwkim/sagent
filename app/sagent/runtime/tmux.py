"""tmux execution layer.

All sessions live on a dedicated tmux socket (`tmux -L sagent` by default) so
they never mix with the operator's own tmux sessions. Users can still attach:
`tmux -L sagent attach -t <session>` (or `sagent attach <run>`).
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

SESSION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
WIDTH, HEIGHT = 200, 50

# Keys a user may send from the web UI (everything else is typed literally).
ALLOWED_KEYS = {
    "Enter", "Escape", "Tab", "BTab", "BSpace", "Up", "Down", "Left", "Right",
    "PageUp", "PageDown", "Home", "End", "C-c", "C-d", "C-l", "C-r", "C-o", "Space",
}

# Never leak sagent's own secrets into agent processes.
_SCRUB_PREFIXES = ("SAGENT_SECRET", "SAGENT_ADMIN")


class TmuxError(RuntimeError):
    pass


def socket_name() -> str:
    return os.environ.get("SAGENT_TMUX_SOCKET", "sagent")


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUB_PREFIXES)}
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    if extra:
        env.update(extra)
    return env


def _check(name: str) -> str:
    if not SESSION_RE.match(name or ""):
        raise TmuxError(f"invalid tmux session name: {name!r}")
    return name


def _tmux(*args: str, input_text: str | None = None, check: bool = True, timeout: int = 10):
    cmd = ["tmux", "-L", socket_name(), "-f", "/dev/null", *args]
    try:
        res = subprocess.run(
            cmd, input=input_text, capture_output=True, text=True, timeout=timeout, env=clean_env()
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TmuxError(f"tmux failed: {exc}") from exc
    if check and res.returncode != 0:
        raise TmuxError((res.stderr or res.stdout or "tmux error").strip())
    return res


def session_name(slug: str, run_id: int) -> str:
    base = re.sub(r"[^a-z0-9-]", "-", slug.lower())
    return _check(f"sagent-{base}"[: 64 - len(str(run_id)) - 1] + f"-{run_id}")


def exists(name: str) -> bool:
    return _tmux("has-session", "-t", f"={_check(name)}", check=False).returncode == 0


def start(name: str, cwd: Path, argv: list[str], log_path: Path | None = None) -> None:
    """Start argv in a detached session.

    argv must be fixed, trusted tokens (tmux itself parses ';' in arguments),
    so callers pass user-influenced data through a spec file, never argv.
    Global options are set in the same tmux invocation so even a process that
    exits immediately leaves its pane (and exit status) behind.
    """
    _check(name)
    if not argv:
        raise TmuxError("empty command")
    if any(a.endswith(";") for a in argv) or str(cwd).endswith(";"):
        raise TmuxError("unsafe argument for tmux")
    # Web users type into these panes: no prefix key, no bindings, no mouse,
    # so nobody can reach tmux commands (choose-tree, command-prompt, run-shell)
    # and hop into another user's session.
    _tmux(
        "start-server", ";",
        "set-option", "-g", "remain-on-exit", "on", ";",
        "set-option", "-g", "history-limit", "20000", ";",
        "set-option", "-g", "prefix", "None", ";",
        "set-option", "-g", "prefix2", "None", ";",
        "set-option", "-g", "mouse", "off", ";",
        "unbind-key", "-a", ";",
        "unbind-key", "-a", "-T", "root", ";",
        "unbind-key", "-a", "-T", "copy-mode", ";",
        "unbind-key", "-a", "-T", "copy-mode-vi", ";",
        "new-session", "-d", "-s", name, "-x", str(WIDTH), "-y", str(HEIGHT), "-c", str(cwd),
        "--", *argv,
    )
    if log_path is not None:
        _tmux("pipe-pane", "-o", "-t", _pane(name), f"cat >> {shlex.quote(str(log_path))}", check=False)


def _pane(name: str) -> str:
    return f"={_check(name)}:"


def status(name: str) -> dict | None:
    """{'dead': bool, 'exit': int|None, 'command': str} or None if no session."""
    res = _tmux(
        "display-message", "-p", "-t", _pane(name),
        "#{pane_dead}|#{pane_dead_status}|#{pane_current_command}", check=False,
    )
    if res.returncode != 0:
        return None
    dead, code, command = (res.stdout.strip().split("|") + ["", "", ""])[:3]
    return {"dead": dead == "1", "exit": int(code) if code.isdigit() else None, "command": command}


def capture(name: str, lines: int = 2000) -> str:
    res = _tmux("capture-pane", "-p", "-J", "-S", f"-{int(lines)}", "-t", _pane(name), check=False)
    if res.returncode != 0:
        return ""
    text = "\n".join(line.rstrip() for line in res.stdout.splitlines()).rstrip()
    return text + "\n" if text else ""


def send_text(name: str, text: str, enter: bool = True) -> None:
    _check(name)
    # Text always travels via stdin into a paste buffer, never as an argv
    # token (tmux would interpret a trailing ';' as a command separator).
    text = text.replace("\r\n", "\n")
    if text:
        buf = f"sagent-{name}"
        _tmux("load-buffer", "-b", buf, "-", input_text=text)
        _tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", _pane(name))
    if enter:
        _tmux("send-keys", "-t", _pane(name), "Enter")


def send_key(name: str, key: str) -> None:
    if key not in ALLOWED_KEYS:
        raise TmuxError(f"key not allowed: {key}")
    _tmux("send-keys", "-t", _pane(name), key)


def kill(name: str) -> None:
    _tmux("kill-session", "-t", f"={_check(name)}", check=False)


def list_sessions(prefix: str = "sagent-") -> list[str]:
    res = _tmux("list-sessions", "-F", "#{session_name}", check=False)
    if res.returncode != 0:
        return []
    return [s for s in res.stdout.split() if s.startswith(prefix)]


def attach_argv(name: str) -> list[str]:
    return ["tmux", "-L", socket_name(), "-f", "/dev/null", "attach", "-t", f"={_check(name)}"]
