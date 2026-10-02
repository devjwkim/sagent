"""Agent adapter interface.

An adapter knows how to build the command line for its CLI, how to turn the
CLI's JSON output into normalised sagent events, and how to render a line for
humans watching the terminal. Process control (start/send/stop) is generic and
lives in the run manager on top of tmux, so adapters stay small.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RunSpec:
    """Everything an adapter needs to build a command."""

    cwd: Path
    prompt_file: Path | None = None
    mode: str = "auto"  # auto (headless JSON) | interactive
    resume_session: str | None = None
    session_id: str | None = None  # pre-assigned id when the CLI supports it
    model: str | None = None
    permission_mode: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    model: str | None = None


class AgentAdapter:
    name = "base"
    binary = ""
    label = ""

    # -- environment ---------------------------------------------------------
    def check_installed(self) -> bool:
        return bool(self.binary and shutil.which(self.binary))

    def check_authenticated(self) -> bool | None:
        """True/False when it can be determined cheaply, None if unknown."""
        return None

    # -- command lines -------------------------------------------------------
    def build_command(self, spec: RunSpec) -> list[str]:
        raise NotImplementedError

    @staticmethod
    def permission_policy(perms: dict) -> dict:
        """Translate harness `permissions` into RunSpec fields for this CLI."""
        return {}

    def prompt_via_stdin(self, spec: RunSpec) -> bool:
        return spec.mode == "auto" and spec.prompt_file is not None

    # -- output --------------------------------------------------------------
    def parse(self, obj: dict) -> list[Event]:
        """Normalise one JSON object emitted by the CLI."""
        return []

    def session_id(self, obj: dict) -> str | None:
        return None

    def usage(self, obj: dict) -> Usage | None:
        """Cumulative/final usage carried by this object, if any."""
        return None

    def outcome(self, obj: dict) -> tuple[bool, str] | None:
        """(success, summary) when this object ends the agent turn."""
        return None

    def render(self, obj: dict) -> str | None:
        """Human readable line for the terminal; None to hide."""
        return None

    def final_text(self, obj: dict) -> str | None:
        """The agent's final answer if this object carries it (full length)."""
        return None


def _int(value: Any) -> int:
    """Token counts from agent JSON: tolerate strings, floats, NaN, negatives."""
    try:
        v = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0
    return v if 0 <= v <= 10_000_000_000 else 0


def short(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


FILE_READ_TOOLS = {"Read", "Glob", "Grep", "LS", "NotebookRead"}
FILE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "BashOutput"}


def tool_event_type(tool: str, phase: str) -> str:
    """Map a tool name to agent.shell.* / agent.file.* / agent.tool.*"""
    if tool in SHELL_TOOLS:
        return f"agent.shell.{phase}"
    if phase == "start" and tool in FILE_WRITE_TOOLS:
        return "agent.file.write"
    if phase == "start" and tool in FILE_READ_TOOLS:
        return "agent.file.read"
    return f"agent.tool.{phase}"
