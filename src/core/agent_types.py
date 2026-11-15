"""Pure data contracts for agent behaviour plugins.

These types carry no behaviour: they describe what a plugin may declare
(profiles, tools) and what the agent loop reports back (events, run results).
They live in ``core`` so a behaviour plugin can depend on the contract without
importing the agent plugin's implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

ToolHandler = Callable[..., str]
"""A tool handler: keyword arguments in, text out."""

# ---------------------------------------------------------------------------
# Declarations (plugin -> agent)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AgentTool:
    """One tool a behaviour plugin wants, with its parameters."""

    name: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentProfile:
    """A named agent configuration registered by a behaviour plugin.

    The plugin declares the tools (and their parameters), prompt, model and
    limits; the agent plugin owns the loop, the tools themselves and the
    context.  ``None`` means "fall back to the service default".
    """

    name: str
    system_prompt: str = ""
    description: str = ""
    tools: list[AgentTool] = field(default_factory=list)
    model: str | None = None
    max_steps: int | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    max_tool_output: int | None = None
    max_history_chars: int | None = None
    final_answer_on_limit: bool = False
    stream: bool = False
    # Extra provider-specific request options (kept for compatibility with
    # plugins that pass things like ``response_format`` through).
    llm_extra: dict[str, Any] = field(default_factory=dict)

    def tool_map(self) -> dict[str, AgentTool]:
        """Return ``name -> AgentTool`` for this profile."""

        return {tool.name: tool for tool in self.tools}

    # The camel-case spelling from the original contract.
    tool_Map = tool_map


# ---------------------------------------------------------------------------
# Observability (agent -> plugin)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AgentEvent:
    """An observable event emitted during a run loop (passed to *on_event*)."""

    type: str
    """One of ``run.started``, ``message``, ``tool_call``, ``tool_result``,
    ``run.finished``, ``error``."""

    session_id: str
    step: int = 0
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentRunResult:
    """Returned by ``AgentService.run`` after the REPL loop finishes."""

    session_id: str
    final_message: str = ""
    steps: int = 0
    tool_calls_made: int = 0
    finish_reason: str = "stop"
    error: str | None = None
    total_tokens: int = 0


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolContext:
    """Everything a tool may need, independent of any plugin."""

    workspace: Any = None
    session_id: str = ""
    streams: Any = None
    extra: dict[str, Any] = field(default_factory=dict)
    # Compatibility surface used by the built-in tools: a session getter, a
    # metadata renderer and an output cap.
    session_getter: Callable[[], Any] | None = None
    meta_getter: Callable[[], str] | None = None
    max_tool_output: int = 8000

    def session(self) -> Any:
        """Current agent session (``None`` when the context has no getter)."""

        if self.session_getter is None:
            return None
        return self.session_getter()

    def meta(self) -> str:
        if self.meta_getter is None:
            return ""
        return str(self.meta_getter())


@dataclass(slots=True)
class ToolSpec:
    """A named tool with JSON Schema parameters and a callable handler.

    A spec is either *bound* (``handler`` set) or *declarative* (``factory``
    plus ``params``); :meth:`bind` resolves the latter against a
    :class:`ToolContext`.
    """

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    handler: ToolHandler | None = None
    factory: Callable[..., Any] | None = None
    params: dict[str, Any] = field(default_factory=dict)

    # -- binding ------------------------------------------------------
    def bind(self, context: ToolContext) -> "ToolSpec":
        """Return a spec whose ``handler`` is bound to *context*."""

        if self.handler is not None:
            return self
        if self.factory is None:
            raise ValueError(f"tool {self.name!r} has neither a handler nor a factory")
        produced = self.factory(context, **dict(self.params))
        if isinstance(produced, ToolSpec):
            if produced.handler is None and produced.factory is not None:
                return produced.bind(context)
            return produced
        if callable(produced):
            return ToolSpec(
                name=self.name,
                description=self.description,
                parameters=dict(self.parameters),
                handler=produced,
            )
        raise TypeError(
            f"tool factory for {self.name!r} returned {type(produced).__name__}"
        )

    # -- schema -------------------------------------------------------
    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    #: Backwards-compatible spelling used by the agent loop.
    def openai_schema(self) -> dict[str, Any]:
        return self.to_openai_schema()

    def __call__(self, **kwargs: Any) -> str:
        if self.handler is None:
            raise RuntimeError(f"tool {self.name!r} is not bound")
        return self.handler(**kwargs)


__all__ = [
    "AgentEvent",
    "AgentProfile",
    "AgentRunResult",
    "AgentTool",
    "ToolContext",
    "ToolSpec",
]