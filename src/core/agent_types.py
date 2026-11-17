"""Application declarations and events for SDK agent sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AgentTool:
    """Select one upstream file tool without changing its schema."""

    name: str


@dataclass(slots=True)
class AgentProfile:
    """Declare a task prompt, runtime role, and allowed tools."""

    name: str
    role: str = "default"
    system_prompt: str = ""
    description: str = ""
    tools: list[AgentTool] = field(default_factory=list)


@dataclass(slots=True)
class AgentEvent:
    """A runtime event with application session metadata."""

    type: str
    session_id: str
    step: int = 0
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentRunResult:
    """Terminal result for one SDK run."""

    session_id: str
    final_message: str = ""
    steps: int = 0
    tool_calls_made: int = 0
    finish_reason: str = "stop"
    error: str | None = None
    total_tokens: int = 0


__all__ = ["AgentEvent", "AgentProfile", "AgentRunResult", "AgentTool"]
