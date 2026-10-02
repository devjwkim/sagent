"""Bridge a WebSocket to `tmux attach` running under a pseudo-terminal.

The browser (xterm.js) sends JSON messages:
  {"type": "input", "data": "..."}          keystrokes (ignored when read-only)
  {"type": "resize", "cols": 120, "rows": 40}
and receives raw terminal output as text frames. Closing the socket only
detaches this client; the tmux session (and the agent) keep running.
"""
from __future__ import annotations

import codecs
import fcntl
import json
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading

from sagent.runtime import tmux


def _set_size(fd: int, cols: int, rows: int) -> None:
    cols = max(20, min(int(cols), 400))
    rows = max(5, min(int(rows), 200))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def bridge(ws, session: str, read_only: bool, on_input=None, still_allowed=None, recheck_s: float = 5.0) -> None:
    """still_allowed() -> (may_view, may_input) is polled every `recheck_s`
    seconds so revoked users (removed, deactivated, logged out) lose the
    terminal without waiting for a reconnect."""
    argv = tmux.attach_argv(session)
    if read_only:
        argv.insert(argv.index("attach") + 1, "-r")
    env = tmux.clean_env({"TERM": "xterm-256color"})
    # No fork() in this multi-threaded server: spawn via subprocess on a pty
    # pair; `setsid --ctty` makes the pty the controlling terminal so the tmux
    # client receives SIGWINCH on resize.
    fd, slave = pty.openpty()
    if shutil.which("setsid"):
        argv = ["setsid", "--ctty", *argv]
    try:
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env,
                                start_new_session=True, close_fds=True)
    finally:
        os.close(slave)
    pid = proc.pid
    stop = threading.Event()

    def pump_output():
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while not stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            try:
                ws.send(decoder.decode(chunk))
            except Exception:
                break
        stop.set()

    reader = threading.Thread(target=pump_output, daemon=True)
    reader.start()
    import time as _time

    next_check = _time.monotonic() + recheck_s
    try:
        _set_size(fd, 160, 45)
        while not stop.is_set():
            if still_allowed is not None and _time.monotonic() >= next_check:
                next_check = _time.monotonic() + recheck_s
                may_view, may_input = still_allowed()
                if not may_view:
                    break
                if not may_input and not read_only:
                    break  # writable attach cannot be downgraded in place: drop it
            msg = ws.receive(timeout=1)
            if msg is None:
                continue
            try:
                data = json.loads(msg)
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") == "resize":
                try:
                    _set_size(fd, data.get("cols", 160), data.get("rows", 45))
                except (OSError, ValueError, TypeError):
                    pass
            elif data.get("type") == "input" and not read_only:
                text = str(data.get("data", ""))[:8192]
                if text:
                    os.write(fd, text.encode("utf-8", errors="replace"))
                    if on_input:
                        on_input(len(text))
    except Exception:
        pass  # client went away
    finally:
        stop.set()
        try:
            os.kill(pid, signal.SIGHUP)  # detach this client only
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            os.close(fd)
        except OSError:
            pass
