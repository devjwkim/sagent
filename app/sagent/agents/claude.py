"""Claude Code adapter.

Automation: `claude -p --output-format stream-json --verbose` with the prompt on stdin.
Interactive: plain `claude` in the tmux pane, optionally `--resume <id>`.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from sagent.agents.base import AgentAdapter, Event, RunSpec, Usage, _int, short, tool_event_type

PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan", "default")
_TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}(\([^()\n]{0,200}\))?$")


class ClaudeAdapter(AgentAdapter):
    name = "claude"
    binary = "claude"
    label = "Claude Code"

    def check_authenticated(self) -> bool | None:
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return True
        home = Path.home()
        if (home / ".claude" / ".credentials.json").exists():
            return True
        return None  # macOS keychain etc. — cannot tell cheaply

    def build_command(self, spec: RunSpec) -> list[str]:
        cmd = [self.binary]
        if spec.mode == "auto":
            cmd += ["-p", "--output-format", "stream-json", "--verbose"]
        if spec.resume_session:
            cmd += ["--resume", spec.resume_session]
        elif spec.session_id:
            cmd += ["--session-id", spec.session_id]
        if spec.model:
            cmd += ["--model", spec.model]
        if spec.permission_mode and spec.permission_mode != "default":
            cmd += ["--permission-mode", spec.permission_mode]
        budget = spec.extra.get("max_budget_usd")
        if spec.mode == "auto" and budget:
            cmd += ["--max-budget-usd", str(budget)]
        allowed = [t for t in spec.extra.get("allowed_tools") or [] if _TOOL_RE.match(t)]
        denied = [t for t in spec.extra.get("disallowed_tools") or [] if _TOOL_RE.match(t)]
        if allowed:
            cmd += ["--allowedTools", *allowed]
        if denied:
            cmd += ["--disallowedTools", *denied]
        return cmd

    @staticmethod
    def permission_policy(perms: dict) -> dict:
        """Map .sagent/harness.yaml `permissions` to Claude Code flags. This is
        the agent's own permission system, not a sandbox."""
        fs = ((perms.get("filesystem") or {}).get("project") or "write")
        shell = (perms.get("shell") or {}).get("allow", True)
        network = (perms.get("network") or {}).get("allow", True)
        return {
            "permission_mode": "acceptEdits" if fs == "write" else "plan",
            "allowed_tools": ["Bash"] if shell else [],
            "disallowed_tools": ([] if shell else ["Bash"]) + ([] if network else ["WebFetch", "WebSearch"]),
        }

    # -- stream-json ---------------------------------------------------------
    def session_id(self, obj: dict) -> str | None:
        return obj.get("session_id") if obj.get("type") in ("system", "result") else None

    def parse(self, obj: dict) -> list[Event]:
        t = obj.get("type")
        out: list[Event] = []
        if t == "system" and obj.get("subtype") == "init":
            out.append(Event("agent.start", {
                "session_id": obj.get("session_id"), "model": obj.get("model"),
                "cwd": obj.get("cwd"), "permission_mode": obj.get("permissionMode"),
            }))
        elif t == "assistant":
            msg = obj.get("message") or {}
            for block in msg.get("content") or []:
                bt = block.get("type")
                if bt == "text" and block.get("text", "").strip():
                    out.append(Event("agent.message", {"text": block["text"][:20000]}))
                elif bt == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input") or {}
                    data = {"tool": name, "tool_use_id": block.get("id"), "input": _tool_input_summary(name, inp)}
                    out.append(Event(tool_event_type(name, "start"), data))
        elif t == "user":
            content = (obj.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    out.append(Event("agent.tool.end", {
                        "tool_use_id": block.get("tool_use_id"),
                        "is_error": bool(block.get("is_error")),
                    }))
        elif t == "result":
            u = self.usage(obj)
            if u:
                out.append(Event("agent.usage", u.__dict__))
            out.append(Event("agent.stop", {
                "subtype": obj.get("subtype"), "is_error": bool(obj.get("is_error")),
                "num_turns": obj.get("num_turns"), "duration_ms": obj.get("duration_ms"),
                "result": short(obj.get("result") or "", 2000),
            }))
        return out

    def usage(self, obj: dict) -> Usage | None:
        if obj.get("type") != "result":
            return None
        u = obj.get("usage") or {}
        models = list((obj.get("modelUsage") or {}).keys())
        return Usage(
            input_tokens=_int(u.get("input_tokens")),
            output_tokens=_int(u.get("output_tokens")),
            cache_read_tokens=_int(u.get("cache_read_input_tokens")),
            cache_write_tokens=_int(u.get("cache_creation_input_tokens")),
            cost_usd=obj.get("total_cost_usd"),
            model=models[0] if models else None,
        )

    def outcome(self, obj: dict) -> tuple[bool, str] | None:
        if obj.get("type") != "result":
            return None
        ok = obj.get("subtype") == "success" and not obj.get("is_error")
        denied = sorted({d.get("tool_name", "?") for d in obj.get("permission_denials") or [] if isinstance(d, dict)})
        if ok and denied:
            # the agent "finished" but could not do the work: never count that as success
            return False, f"permission denied for: {', '.join(denied)}"
        return ok, short(obj.get("result") or obj.get("subtype") or "", 500)

    def final_text(self, obj: dict) -> str | None:
        if obj.get("type") == "result" and isinstance(obj.get("result"), str):
            return obj["result"]
        return None

    def render(self, obj: dict) -> str | None:
        t = obj.get("type")
        if t == "system" and obj.get("subtype") == "init":
            return f"● claude session {obj.get('session_id', '')} · model {obj.get('model', '?')}"
        if t == "assistant":
            lines = []
            for block in (obj.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    lines.append(block["text"].rstrip())
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    lines.append(f"▶ {name} {_tool_input_summary(name, block.get('input') or {})}")
            return "\n".join(lines) or None
        if t == "user":
            content = (obj.get("message") or {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_result" and block.get("is_error"):
                        return "  ✗ tool error"
            return None
        if t == "result":
            u = self.usage(obj)
            cost = f" · ${obj['total_cost_usd']:.4f}" if isinstance(obj.get("total_cost_usd"), (int, float)) else ""
            tokens = f" · in {u.input_tokens} / out {u.output_tokens}" if u else ""
            return f"■ {obj.get('subtype', 'done')} · turns {obj.get('num_turns', '?')}{tokens}{cost}"
        return None


def _tool_input_summary(name: str, inp: dict) -> str:
    for key in ("command", "file_path", "path", "pattern", "url", "description"):
        if key in inp:
            return short(inp[key], 200)
    return short(inp, 120) if inp else ""
