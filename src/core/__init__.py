"""Core framework: streams, events, plugins, services and UI."""

from __future__ import annotations

from .agent_types import (
    AgentEvent,
    AgentProfile,
    AgentRunResult,
    AgentTool,
    ToolContext,
    ToolSpec,
)
from .events import (
    CLOCK_TICK,
    EVENT_ERROR,
    SESSION_STARTED,
    SESSION_STOPPED,
    STREAM_APPENDED,
    Clock,
    Event,
    EventBus,
    EventType,
    Subscription,
)
from .meta import MetaRegistry
from .plugin import (
    Plugin,
    PluginContext,
    PluginError,
    PluginManager,
    resolve_plugin_spec,
)
from .runtime import LLMProxy, Runtime, build_runtime
from .services import ServiceError, ServiceNotFound, ServiceRegistry
from .settings_spec import SECRET_MASK, SETTING_TYPES, SettingsSpec
from .streams import (
    AudioChunk,
    AudioStream,
    SessionInfo,
    SourceProgress,
    StreamHub,
    StreamSet,
    TextItem,
    TextStream,
    VideoFrame,
    VideoStream,
)
from .ui import ActionResult, PageHandle, TabHandle, UIManager, render_markdown

__all__ = [
    "ActionResult",
    "AgentEvent",
    "AgentProfile",
    "AgentRunResult",
    "AgentTool",
    "AudioChunk",
    "AudioStream",
    "CLOCK_TICK",
    "Clock",
    "EVENT_ERROR",
    "Event",
    "EventBus",
    "EventType",
    "LLMProxy",
    "MetaRegistry",
    "PageHandle",
    "Plugin",
    "PluginContext",
    "PluginError",
    "PluginManager",
    "Runtime",
    "SECRET_MASK",
    "SESSION_STARTED",
    "SESSION_STOPPED",
    "SETTING_TYPES",
    "STREAM_APPENDED",
    "ServiceError",
    "ServiceNotFound",
    "ServiceRegistry",
    "SessionInfo",
    "SettingsSpec",
    "SourceProgress",
    "StreamHub",
    "StreamSet",
    "Subscription",
    "TabHandle",
    "TextItem",
    "TextStream",
    "ToolContext",
    "ToolSpec",
    "UIManager",
    "VideoFrame",
    "VideoStream",
    "build_runtime",
    "render_markdown",
    "resolve_plugin_spec",
]
