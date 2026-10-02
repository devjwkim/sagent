"""Process wrapper executed inside the tmux pane.

    python -m sagent.runtime.wrapper --spec DIR/spec.json

spec.json: {"provider", "cmd": [...], "stdin": path|null, "tty": bool, "env": {...}}.
Passing the command through a file keeps tmux's own argument parsing (which
treats ';' specially) and `ps` output away from user-influenced data.

- provider `shell`: runs argv, streams output to the terminal unchanged.
- agent providers: reads the CLI's JSONL stdout, appends every line verbatim
  to DIR/agent.jsonl and prints a human-readable rendering to the terminal.
Always writes DIR/exit.json with the exit code when the child finishes.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _write_exit(run_dir: Path, code: int, started: float) -> None:
    data = {"code": code, "duration_ms": int((time.time() - started) * 1000), "ended_at": time.time()}
    tmp = run_dir / "exit.json.tmp"
    tmp.write_text(json.dumps(data))
    os.replace(tmp, run_dir / "exit.json")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    spec_path = Path(ap.parse_args(argv).spec)
    spec = json.loads(spec_path.read_text())
    run_dir = spec_path.parent
    cmd = [str(c) for c in spec["cmd"]]
    provider = spec["provider"]
    os.environ.update({str(k): str(v) for k, v in (spec.get("env") or {}).items()})
    started = time.time()
    if spec.get("tty"):
        stdin = None
    else:
        stdin = open(spec["stdin"], "rb") if spec.get("stdin") else subprocess.DEVNULL

    if provider == "shell":
        print(f"$ {' '.join(cmd)}", flush=True)
        try:
            proc = subprocess.Popen(cmd, stdin=stdin)
        except OSError as exc:
            print(f"✗ cannot start: {exc}", flush=True)
            _write_exit(run_dir, 127, started)
            return 127
        _forward_signals(proc)
        code = proc.wait()
        print(f"\n■ exit {code}", flush=True)
        _write_exit(run_dir, code, started)
        return code

    from sagent import agents

    adapter = agents.get(provider)
    try:
        proc = subprocess.Popen(cmd, stdin=stdin, stdout=subprocess.PIPE, bufsize=0)
    except OSError as exc:
        print(f"✗ cannot start {cmd[0]}: {exc}", flush=True)
        _write_exit(run_dir, 127, started)
        return 127
    _forward_signals(proc)
    with open(run_dir / "agent.jsonl", "ab") as raw:
        for line in iter(proc.stdout.readline, b""):
            raw.write(line if line.endswith(b"\n") else line + b"\n")
            raw.flush()
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                print(text, flush=True)
                continue
            try:
                rendered = adapter.render(obj) if isinstance(obj, dict) else None
            except Exception:  # rendering must never kill the run
                rendered = None
            if rendered:
                print(rendered, flush=True)
    code = proc.wait()
    print(f"\n■ {adapter.name} exited with {code}", flush=True)
    _write_exit(run_dir, code, started)
    return code


def _forward_signals(proc: subprocess.Popen) -> None:
    def handler(signum, _frame):
        try:
            proc.send_signal(signum)
        except OSError:
            pass

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
