"""Background workers for the web process (kept out of create_app so the
reloader parent never starts a second copy)."""
from __future__ import annotations

import sys
import threading
import time

_started = False
_lock = threading.Lock()


def _loop(name: str, fn, interval: float) -> None:
    while True:
        try:
            fn()
        except Exception as exc:  # never let a worker die
            print(f"sagent worker {name}: {exc}", file=sys.stderr)
        time.sleep(interval)


def start() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    from sagent.core import runs

    runs.recover()
    for name, fn, interval in workers():
        threading.Thread(target=_loop, args=(name, fn, interval), name=f"sagent-{name}", daemon=True).start()


def workers():
    from sagent.core import loops, runs

    return [("runs", runs.tick, 1.0), ("loops", loops.tick, 1.0),
            ("retention", runs.purge_old_logs, 6 * 3600.0)]
