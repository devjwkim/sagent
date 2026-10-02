"""Agent adapter registry. New CLIs are added by registering an adapter here."""
from __future__ import annotations

from sagent.agents.base import AgentAdapter, Event, RunSpec, Usage
from sagent.agents.claude import ClaudeAdapter
from sagent.agents.codex import CodexAdapter

_REGISTRY: dict[str, AgentAdapter] = {
    "claude": ClaudeAdapter(),
    "codex": CodexAdapter(),
}


def get(name: str) -> AgentAdapter:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown agent provider: {name}") from None


def names() -> list[str]:
    return list(_REGISTRY)


def register(adapter: AgentAdapter) -> None:
    _REGISTRY[adapter.name] = adapter


__all__ = ["AgentAdapter", "Event", "RunSpec", "Usage", "get", "names", "register"]
