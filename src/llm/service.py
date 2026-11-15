"""LLMService — the single entry point for all LLM calls.

Profiles map logical roles (``agent``, ``vlm``, ``stt``) to provider
configurations.  Configuration is fetched lazily from a callback so the service
stays decoupled from any particular settings plugin.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Iterator, Union

from .errors import LLMError
from .provider import Provider, ProviderRegistry
from .trace import LLMTrace, TraceCall
from .types import (
    ChatRequest,
    ChatResponse,
    ImagePart,
    Message,
    ProviderConfig,
    StreamEvent,
    TextPart,
    ToolDef,
    Usage,
)

logger = logging.getLogger("lecture_agent.llm.service")

ConfigCallback = Callable[[str], ProviderConfig]

# Allow callers to pass Message objects or OpenAI-style dicts.
MessageInput = Union[Message, dict[str, Any]]

_DEFAULT_CONFIG = ProviderConfig()


def _coerce_message(msg: MessageInput) -> Message:
    """Accept both ``Message`` objects and OpenAI-style dicts."""
    if isinstance(msg, Message):
        return msg
    if isinstance(msg, dict):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            parts: list[TextPart | ImagePart] = []
            for part in content:
                if isinstance(part, TextPart | ImagePart):
                    parts.append(part)
                elif isinstance(part, dict):
                    kind = part.get("type")
                    if kind == "text":
                        parts.append(TextPart(text=str(part.get("text", ""))))
                    elif kind == "image_url":
                        image = part.get("image_url")
                        url = image.get("url") if isinstance(image, dict) else image
                        if url:
                            parts.append(ImagePart(url=str(url)))
                    else:
                        parts.append(TextPart(text=str(part)))
                else:
                    parts.append(TextPart(text=str(part)))
            content = parts
        name = msg.get("name")
        tool_calls_raw = msg.get("tool_calls", [])
        from .types import ToolCall as TC

        tool_calls = [
            TC(
                id=tc.get("id", ""),
                name=tc.get("function", {}).get("name", ""),
                arguments=tc.get("function", {}).get("arguments", {}),
            )
            for tc in tool_calls_raw
        ]
        return Message(
            role=role,
            content=content,
            name=name,
            tool_calls=tool_calls,
            tool_call_id=msg.get("tool_call_id"),
        )
    raise TypeError(f"expected Message or dict, got {type(msg).__name__}")


def _coerce_tool(tool: ToolDef | dict[str, Any]) -> ToolDef:
    """Accept both ``ToolDef`` and OpenAI-style function dicts."""
    if isinstance(tool, ToolDef):
        return tool
    if isinstance(tool, dict):
        fn = tool.get("function")
        if isinstance(fn, dict):
            return ToolDef(
                name=str(fn.get("name", "")),
                description=str(fn.get("description", "")),
                parameters=dict(fn.get("parameters") or {}),
            )
        return ToolDef(
            name=str(tool.get("name", "")),
            description=str(tool.get("description", "")),
            parameters=dict(tool.get("parameters") or {}),
        )
    raise TypeError(f"expected ToolDef or dict, got {type(tool).__name__}")


class LLMService:
    """Multi-provider LLM facade.

    Every collaborator is private state: callers configure the service through
    the constructor and then only use the eight public methods
    (``register_provider``, ``unregister_provider``, ``chat``, ``stream``,
    ``config_for``, ``usage_snapshot``, ``info``, ``close``).

    Parameters
    ----------
    config_provider : callable
        ``(profile: str) -> ProviderConfig``.  Called on every request so
        config changes take effect immediately.
    default_provider : str
        Fallback provider name when a profile config doesn't specify one.
    on_event : callable | None
        ``(event_type: str, payload: dict) -> None`` callback for observability
        (``llm.request``, ``llm.response``, ``llm.error``).
    trace_dir : path-like | None
        When set, every call is written verbatim to a JSON file under this
        directory (see :mod:`llm.trace`).  ``None`` disables on-disk tracing.
    registry : ProviderRegistry | None
        Shared provider registry; a private one is created when omitted.
    """

    def __init__(
        self,
        config_provider: ConfigCallback | None = None,
        default_provider: str = "openai",
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        trace_dir: str | os.PathLike[str] | None = None,
        registry: ProviderRegistry | None = None,
    ) -> None:
        self._config_provider: ConfigCallback = (
            config_provider if config_provider is not None else (lambda _: _DEFAULT_CONFIG)
        )
        self._default_provider = str(default_provider or "openai")
        self._on_event = on_event
        self._trace_dir = trace_dir
        self._registry = registry if registry is not None else ProviderRegistry()
        self._trace: LLMTrace | None = (
            LLMTrace(trace_dir) if trace_dir is not None else None
        )
        # Per-model token totals: ``requests``, ``input_tokens``,
        # ``output_tokens``, ``cache_read_tokens``.  Counted, not priced:
        # pricing depends on the plan and peak hours, so it is left to a later
        # accounting layer.
        self._usage_by_model: dict[str, dict[str, int]] = {}
        self._usage_lock = threading.Lock()

    def register_provider(self, name: str, provider: Provider) -> None:
        self._registry.register(name, provider)

    def unregister_provider(self, name: str) -> None:
        """Stop exposing a provider (hot-unplug support)."""

        self._registry.unregister(name)

    def _record_usage(
        self, model: str, usage: Usage, trace_path: Any = None
    ) -> dict[str, int]:
        """Add one response to the per-model totals and log the request line."""

        key = model or "unknown"
        with self._usage_lock:
            totals = self._usage_by_model.setdefault(
                key,
                {
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                },
            )
            totals["requests"] += 1
            totals["input_tokens"] += usage.prompt_tokens
            totals["output_tokens"] += usage.completion_tokens
            totals["cache_read_tokens"] += getattr(usage, "cached_tokens", 0)
            snapshot = dict(totals)

        logger.info(
            "llm usage model=%s input=%d output=%d cache_read=%d | "
            "total input=%d output=%d cache_read=%d | trace=%s",
            key,
            usage.prompt_tokens,
            usage.completion_tokens,
            getattr(usage, "cached_tokens", 0),
            snapshot["input_tokens"],
            snapshot["output_tokens"],
            snapshot["cache_read_tokens"],
            trace_path or "-",
        )
        return snapshot

    def usage_snapshot(self) -> dict[str, dict[str, int]]:
        """Copy of the per-model token totals (safe to hand to the UI)."""

        with self._usage_lock:
            return {model: dict(totals) for model, totals in self._usage_by_model.items()}

    def _provider_for(self, config: ProviderConfig) -> Provider:
        provider = self._registry.require(config.provider or self._default_provider)
        reconfigure = getattr(provider, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(config)
        return provider


    # ------------------------------------------------------------------
    # chat / stream
    # ------------------------------------------------------------------
    def chat(
        self,
        *,
        profile: str = "agent",
        messages: list[MessageInput],
        model: str | None = None,
        tools: list[ToolDef] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        config = self.config_for(profile)
        provider = self._provider_for(config)
        coerced = [_coerce_message(m) for m in messages]

        request = ChatRequest(
            model=model or config.model,
            messages=coerced,
            tools=[_coerce_tool(t) for t in (tools or [])],
            tool_choice=tool_choice,
            temperature=temperature if temperature is not None else config.temperature,
            max_tokens=max_tokens if max_tokens is not None else config.max_tokens,
            response_format=response_format,
            extra=dict(extra or {}),
            stream=False,
            session_id=session_id,
            parent_session_id=parent_session_id,
        )

        if self._on_event:
            self._on_event("llm.request", {
                "profile": profile,
                "model": request.model,
                "message_count": len(coerced),
                "tool_count": len(request.tools),
                "stream": False,
                "session_id": session_id,
            })

        call = self._trace_start("chat", profile, request, config)
        try:
            response = provider.chat(request)
        except BaseException as exc:  # noqa: BLE001 - re-raised after tracing
            if call is not None:
                call.fail(exc)
            if isinstance(exc, LLMError) and self._on_event:
                self._on_event("llm.error", {"profile": profile, "phase": "chat"})
            raise

        totals = self._record_usage(
            response.model or request.model or "",
            response.usage,
            call.path if call is not None else None,
        )
        if call is not None:
            call.finish(response)
        if self._on_event:
            self._on_event("llm.response", {
                "profile": profile,
                "model": response.model,
                "finish_reason": response.finish_reason,
                "content_len": len(response.content),
                "tool_calls": len(response.tool_calls),
                "session_id": session_id,
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                    "cached_tokens": response.usage.cached_tokens,
                },
                "usage_totals": totals,
            })
        return response

    def stream(
        self,
        *,
        profile: str = "agent",
        messages: list[MessageInput],
        model: str | None = None,
        tools: list[ToolDef] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        **kwargs: Any,
    ) -> Iterator[StreamEvent]:
        config = self.config_for(profile)
        provider = self._provider_for(config)
        coerced = [_coerce_message(m) for m in messages]

        request = ChatRequest(
            model=model or config.model,
            messages=coerced,
            tools=[_coerce_tool(t) for t in (tools or [])],
            tool_choice=tool_choice,
            temperature=temperature if temperature is not None else config.temperature,
            max_tokens=max_tokens if max_tokens is not None else config.max_tokens,
            response_format=response_format,
            extra=dict(extra or {}),
            stream=True,
            session_id=session_id,
            parent_session_id=parent_session_id,
        )

        if self._on_event:
            self._on_event("llm.request", {
                "profile": profile,
                "model": request.model,
                "message_count": len(coerced),
                "tool_count": len(request.tools),
                "stream": True,
                "session_id": session_id,
            })

        call = self._trace_start("stream", profile, request, config)
        try:
            for event in provider.stream(request):
                if call is not None:
                    if event.type == "text":
                        call.on_text(event.text)
                    elif event.type == "reasoning":
                        call.on_reasoning(event.text)
                    elif event.type == "tool_call":
                        call.on_tool_call(event.tool_call)
                if event.type == "done" and event.response is not None:
                    usage = event.response.usage
                    self._record_usage(
                        event.response.model or request.model or "",
                        usage,
                        call.path if call is not None else None,
                    )
                    if call is not None:
                        call.finish(event.response)
                if event.type == "error":
                    if call is not None:
                        call.on_error(event.error)
                    if self._on_event:
                        self._on_event("llm.error", {
                            "profile": profile,
                            "phase": "stream",
                            "error": str(event.error) if event.error else "",
                        })
                yield event
        except GeneratorExit:
            if call is not None:
                call.abort("consumer closed the stream early")
            raise
        except BaseException as exc:  # noqa: BLE001 - re-raised after tracing
            if call is not None:
                call.fail(exc)
            if isinstance(exc, LLMError) and self._on_event:
                self._on_event("llm.error", {"profile": profile, "phase": "stream"})
            raise
        finally:
            if call is not None:
                call.close()

    def _trace_start(
        self,
        kind: str,
        profile: str,
        request: ChatRequest,
        config: ProviderConfig,
    ) -> TraceCall | None:
        trace = self._trace
        if trace is None:
            return None
        try:
            return trace.start(
                kind=kind,
                profile=profile,
                request=request,
                config=config,
                provider=config.provider or self._default_provider,
            )
        except Exception:  # noqa: BLE001 - tracing must never break a call
            logger.exception("failed to start LLM trace for profile %s", profile)
            return None

    # ------------------------------------------------------------------
    # config
    # ------------------------------------------------------------------
    def config_for(self, profile: str) -> ProviderConfig:
        """Resolve config for *profile* (e.g. ``"agent"``, ``"vlm"``, ``"stt"``).

        The *vlm* profile overrides ``model`` with ``vlm_model`` when set.
        """
        base = self._config_provider(profile)
        config = ProviderConfig(
            base_url=base.base_url or _DEFAULT_CONFIG.base_url,
            api_key=base.api_key or _DEFAULT_CONFIG.api_key,
            model=base.model or _DEFAULT_CONFIG.model,
            provider=base.provider or _DEFAULT_CONFIG.provider,
            temperature=(
                base.temperature
                if base.temperature is not None
                else _DEFAULT_CONFIG.temperature
            ),
            max_tokens=(
                base.max_tokens
                if base.max_tokens is not None
                else _DEFAULT_CONFIG.max_tokens
            ),
            timeout=base.timeout or _DEFAULT_CONFIG.timeout,
            max_retries=(
                base.max_retries
                if base.max_retries is not None
                else _DEFAULT_CONFIG.max_retries
            ),
            extra_headers=dict(base.extra_headers),
            extra_body=dict(base.extra_body),
        )
        return config

    # ------------------------------------------------------------------
    # lifecycle / info
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Close every registered provider."""
        self._registry.close_all()

    def info(self) -> dict[str, Any]:
        return {
            "registered": True,
            "provider": self._default_provider,
            "providers": self._registry.names(),
            "default_provider": self._default_provider,
            "usage": self.usage_snapshot(),
        }