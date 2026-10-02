"""OpenAI Codex CLI adapter.

Automation: `codex exec --json -` (prompt on stdin), resume with
`codex exec resume <thread_id> --json -`. Interactive: `codex` in the pane.
Event names follow the `codex exec --json` JSONL stream (thread.started,
turn.completed, item.started / item.completed with item.type).
"""
from __future__ import annotations

import os
from pathlib import Path

from sagent.agents.base import AgentAdapter, Event, RunSpec, Usage, _int, short

SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")


class CodexAdapter(AgentAdapter):
    name = "codex"
    binary = "codex"
    label = "Codex CLI"

    def check_authenticated(self) -> bool | None:
        if os.environ.get("OPENAI_API_KEY"):
            return True
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        return True if (codex_home / "auth.json").exists() else None

    def build_command(self, spec: RunSpec) -> list[str]:
        if spec.mode != "auto":
            cmd = [self.binary]
            if spec.resume_session:
                cmd += ["resume", spec.resume_session]
            if spec.model:
                cmd += ["--model", spec.model]
            return cmd
        cmd = [self.binary, "exec"]
        if spec.resume_session:
            cmd += ["resume", spec.resume_session]
        cmd += ["--json", "--skip-git-repo-check"]
        if spec.model:
            cmd += ["--model", spec.model]
        sandbox = spec.permission_mode if spec.permission_mode in SANDBOX_MODES else "workspace-write"
        if not spec.resume_session:
            cmd += ["--sandbox", sandbox]
        cmd.append("-")  # read prompt from stdin
        return cmd

    @staticmethod
    def permission_policy(perms: dict) -> dict:
        fs = ((perms.get("filesystem") or {}).get("project") or "write")
        return {"permission_mode": "workspace-write" if fs == "write" else "read-only"}

    def session_id(self, obj: dict) -> str | None:
        return obj.get("thread_id") if obj.get("type") == "thread.started" else None

    def parse(self, obj: dict) -> list[Event]:
        t = obj.get("type", "")
        item = obj.get("item") or {}
        it = item.get("type")
        if t == "thread.started":
            return [Event("agent.start", {"session_id": obj.get("thread_id")})]
        if t in ("item.started", "item.completed"):
            phase = "start" if t == "item.started" else "end"
            if it == "agent_message" and phase == "end":
                return [Event("agent.message", {"text": (item.get("text") or "")[:20000]})]
            if it == "command_execution":
                data = {"command": short(item.get("command", ""), 300)}
                if phase == "end":
                    data["exit_code"] = item.get("exit_code")
                return [Event(f"agent.shell.{phase}", data)]
            if it == "file_change" and phase == "end":
                files = [c.get("path") for c in item.get("changes") or []]
                return [Event("agent.file.write", {"files": files[:50]})]
            if it in ("mcp_tool_call", "web_search"):
                return [Event(f"agent.tool.{phase}", {"tool": item.get("tool") or it})]
            if it == "error" and phase == "end":
                return [Event("agent.warning", {"text": short(item.get("message") or "", 500)})]
            return []
        if t == "turn.completed":
            u = self.usage(obj)
            return [Event("agent.usage", u.__dict__), Event("agent.stop", {"is_error": False})] if u else []
        if t == "turn.failed":
            err = obj.get("error")
            msg = err.get("message") if isinstance(err, dict) else obj.get("message")
            return [Event("agent.stop", {"is_error": True, "result": short(msg or t, 2000)})]
        if t == "error":
            # Codex 0.16x emits top-level `error` for transient problems
            # (e.g. "Reconnecting... 2/5"); the turn only fails on turn.failed.
            return [Event("agent.warning", {"text": short(obj.get("message") or "", 500)})]
        return []

    def usage(self, obj: dict) -> Usage | None:
        if obj.get("type") != "turn.completed":
            return None
        u = obj.get("usage") or {}
        return Usage(
            input_tokens=_int(u.get("input_tokens")),
            output_tokens=_int(u.get("output_tokens")),
            cache_read_tokens=_int(u.get("cached_input_tokens")),
        )

    def outcome(self, obj: dict) -> tuple[bool, str] | None:
        t = obj.get("type")
        if t == "turn.completed":
            return True, "turn completed"
        if t == "turn.failed":
            err = obj.get("error")
            return False, short(err.get("message") if isinstance(err, dict) else (obj.get("message") or t), 500)
        return None

    def final_text(self, obj: dict) -> str | None:
        item = obj.get("item") or {}
        if obj.get("type") == "item.completed" and item.get("type") == "agent_message":
            return item.get("text") or None
        return None

    def render(self, obj: dict) -> str | None:
        t = obj.get("type", "")
        item = obj.get("item") or {}
        it = item.get("type")
        if t == "thread.started":
            return f"● codex thread {obj.get('thread_id', '')}"
        if t == "item.started" and it == "command_execution":
            return f"$ {short(item.get('command', ''), 300)}"
        if t == "item.completed":
            if it == "agent_message":
                return (item.get("text") or "").rstrip() or None
            if it == "command_execution":
                code = item.get("exit_code")
                return f"  {'✓' if code == 0 else '✗'} exit {code}"
            if it == "file_change":
                return "✎ " + ", ".join(c.get("path", "?") for c in item.get("changes") or [])
            if it == "reasoning":
                return f"· {short(item.get('text', ''), 200)}"
        if t == "turn.completed":
            u = self.usage(obj)
            return f"■ turn completed · in {u.input_tokens} / out {u.output_tokens}"
        if t == "turn.failed":
            return f"✗ {self.outcome(obj)[1]}"
        if t == "error":
            return f"! {short(obj.get('message') or '', 200)}"
        if t == "item.completed" and it == "error":
            return f"! {short(item.get('message') or '', 200)}"
        return None
