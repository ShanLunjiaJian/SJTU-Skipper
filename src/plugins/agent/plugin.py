"""Manage application sessions and forward SDK events to the UI."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Mapping
from contextlib import AbstractContextManager

from core.file_access import publication_lock
from core.agent_types import AgentEvent, AgentProfile, AgentRunResult, AgentTool
from core.plugin import Plugin, PluginContext
from core.settings_spec import SettingsSpec

from .prompts import DEFAULT_SYSTEM_PROMPT
from .session import AgentSession
from core.workspace import FileWorkspace

_FILE_TOOLS = ("read", "grep", "glob", "edit", "write")


class AgentService:
    """Keep UI metadata while the SDK owns conversations and tools."""

    def __init__(
        self,
        ctx: PluginContext,
        default_system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        settings_getter: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self._ctx = ctx
        self._runtime = ctx.require("llm")
        self._default_system_prompt = default_system_prompt
        self._settings_getter = settings_getter
        self._sessions: dict[str, AgentSession] = {}
        self._agents: dict[str, AgentProfile] = {}
        self._lock = threading.RLock()

    def _setting(self, key: str, fallback: Any) -> Any:
        return (
            self._settings_getter(key, fallback) if self._settings_getter else fallback
        )

    def register_agent(self, profile: AgentProfile) -> None:
        if not profile.name:
            raise ValueError("Agent profile needs a name")
        with self._lock:
            self._agents[profile.name] = profile

    def create_session(
        self,
        name: str,
        *,
        agent: str | AgentProfile | None = None,
        role: str | None = None,
        system_prompt: str | None = None,
        files: Mapping[str, str | Path] | FileWorkspace | None = None,
        workspace: FileWorkspace | None = None,
        tools: list[str | AgentTool] | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> AgentSession:
        if isinstance(agent, str):
            with self._lock:
                profile = self._agents.get(agent)
            if profile is None:
                raise KeyError(f"Agent profile {agent!r} is not registered")
        else:
            profile = agent
        ws = (
            workspace
            if workspace is not None
            else (files if isinstance(files, FileWorkspace) else FileWorkspace(files))
        )
        declarations = (
            tools
            if tools is not None
            else (profile.tools if profile else list(_FILE_TOOLS))
        )
        names = tuple(
            item.name if isinstance(item, AgentTool) else item for item in declarations
        )
        unknown = set(names) - set(_FILE_TOOLS)
        if unknown:
            raise ValueError(f"Unsupported tools: {', '.join(sorted(unknown))}")
        prompt = (
            system_prompt
            if system_prompt is not None
            else (
                profile.system_prompt
                if profile
                else self._setting("agent.system_prompt", self._default_system_prompt)
            )
        )
        session = AgentSession(
            name=name,
            agent=profile.name if profile else "",
            role=role or (profile.role if profile else "default"),
            system_prompt=prompt,
            workspace=ws,
            tools=names,
            meta=dict(meta or {}),
        )
        self._create_runtime_session(session)
        with self._lock:
            self._sessions[session.id] = session
        return session

    def _create_runtime_session(self, session: AgentSession) -> None:
        result = self._runtime.call(
            "create_session",
            id=session.id,
            role=session.role,
            prompt=session.system_prompt,
            files={
                name: str(path) for name, path in session.workspace.to_dict().items()
            },
            readonly=list(session.workspace.readonly),
            tools=list(session.tools),
            meta=session.meta,
        )
        session.model = str(result["model"])
        session.runtime_ready = True

    def get_session(self, session_id: str) -> AgentSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def _require_session(self, session_id: str) -> AgentSession:
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(f"Session {session_id!r} not found")
        return session

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [session.to_dict() for session in self._sessions.values()]

    def cancel(self, session_id: str) -> None:
        session = self._require_session(session_id)
        session.cancelled.set()
        self._runtime.cancel(session_id)

    def close(self, session_id: str) -> None:
        session = self.get_session(session_id)
        if session is None:
            return
        try:
            self.cancel(session_id)
        finally:
            try:
                with session.lock:
                    self._runtime.call("close_session", session_id=session_id)
            finally:
                with self._lock:
                    self._sessions.pop(session_id, None)

    def run(
        self,
        session_id: str,
        user_input: str,
        *,
        on_event: Callable[[AgentEvent], None] | None = None,
        write_guard: Callable[[Path], AbstractContextManager] | None = None,
    ) -> AgentRunResult:
        session = self._require_session(session_id)
        if not session.lock.acquire(blocking=False):
            return AgentRunResult(
                session_id=session_id, error="Session is already running"
            )
        result = AgentRunResult(session_id=session_id)
        calls = 0
        try:
            if session.cancelled.is_set():
                raise RuntimeError("Agent run cancelled")
            if not session.runtime_ready:
                self._create_runtime_session(session)
            self._emit("agent.run.started", session, 0, on_event)
            completed = False
            for frame in self._runtime.stream(
                "run", session_id=session_id, instruction=user_input
            ):
                if frame.get("type") == "result":
                    values = frame.get("result") or {}
                    result = AgentRunResult(
                        session_id=session_id,
                        final_message=str(values.get("final_message") or ""),
                        steps=int(values.get("steps") or 0),
                        error=values.get("error"),
                        total_tokens=int(values.get("total_tokens") or 0),
                        tool_calls_made=calls,
                        finish_reason=str(values.get("finish_reason") or "stop"),
                    )
                    completed = True
                    continue
                if session.cancelled.is_set():
                    continue
                if frame.get("type") != "event":
                    continue
                name = str(frame.get("name") or "")
                data = dict(frame.get("data") or {})
                step = int(data.pop("step", 0))
                data.pop("session_id", None)
                session.meta["step"] = step
                if name == "agent.execution":
                    model = (data.get("metrics") or {}).get("model")
                    if model is not None:
                        session.model = str(model)
                elif name == "agent.tool_call":
                    calls += 1
                elif name == "agent.file":
                    logical = str(data.get("file") or "")
                    if logical not in session.workspace:
                        raise PermissionError(
                            f"Runtime changed an unauthorized file: {logical}"
                        )
                    path = session.workspace.path(logical)
                    guard = (
                        write_guard(path)
                        if write_guard
                        else publication_lock(path.parent)
                    )
                    with guard:
                        if (
                            "previous" in data
                            and path.read_text(encoding="utf-8") != data["previous"]
                        ):
                            raise RuntimeError(
                                f"Document changed outside this session: {logical}"
                            )
                        session.workspace.write(logical, str(data["content"]))
                self._emit(name, session, step, on_event, **data)
                if name == "agent.file":
                    self._ctx.ui.refresh(f"agent.file:{data.get('path', '')}")
            if not completed:
                raise RuntimeError("Agent runtime ended without a result")
            if session.cancelled.is_set():
                result.error = "Agent run cancelled"
        except Exception as exc:
            try:
                self._runtime.cancel(session_id)
            except Exception:
                pass  # Preserve the run failure if cancellation also fails.
            session.runtime_ready = False
            result.error = str(exc)
        finally:
            try:
                if result.error:
                    self._emit(
                        "agent.error",
                        session,
                        result.steps,
                        on_event,
                        error=result.error,
                    )
                self._emit(
                    "agent.run.finished",
                    session,
                    result.steps,
                    on_event,
                    content=result.final_message,
                    error=result.error,
                )
            finally:
                session.lock.release()
        return result

    def _emit(self, event_type, session, step, on_event, **data):
        self._ctx.events.emit(event_type, session_id=session.id, step=step, **data)
        if on_event is not None:
            on_event(
                AgentEvent(type=event_type, session_id=session.id, step=step, data=data)
            )


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

_AGENT_SETTINGS_GROUP = "Agent"


class AgentPlugin(Plugin):
    """Register the agent task service and its settings."""

    id = "llm-agent"
    requires = ("settings", "llm")
    provides = ("agent",)

    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.service: AgentService | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        settings = ctx.require("settings")

        # --- register settings ---
        agent_specs = [
            SettingsSpec(
                key="agent.system_prompt",
                label="系统提示词",
                type="text",
                default=DEFAULT_SYSTEM_PROMPT,
                group=_AGENT_SETTINGS_GROUP,
                description="新建 Agent 会话的默认系统提示词．",
            ),
        ]
        for spec in agent_specs:
            try:
                settings.register(spec, plugin_id=self.id)
            except Exception:
                ctx.logger.warning("settings register failed for %s", spec.key)

        # --- build service ---
        default_system_prompt = str(
            settings.get("agent.system_prompt", DEFAULT_SYSTEM_PROMPT)
        )

        self.service = AgentService(
            ctx=ctx,
            default_system_prompt=default_system_prompt,
            settings_getter=settings.get,
        )
        ctx.register_service("agent", self.service)

        # --- meta ---
        ctx.meta.register(
            "agent",
            lambda: {
                "sessions": len(self.service.list_sessions()),
            },
            plugin_id=self.id,
        )

        # --- event definitions ---
        for evt_name, evt_desc in [
            ("agent.run.started", "Agent run started"),
            ("agent.delta", "Streaming text delta"),
            ("agent.reasoning", "Streaming reasoning delta"),
            ("agent.message", "Assistant message received"),
            ("agent.tool_call", "Tool call issued"),
            ("agent.tool_result", "Tool result received"),
            ("agent.file", "Authorized file changed"),
            ("agent.run.finished", "Agent run finished"),
            ("agent.error", "Agent error"),
        ]:
            ctx.events.define(evt_name, evt_desc)

        ctx.logger.info("agent plugin ready; %s sessions", 0)

    def teardown(self) -> None:
        ctx = self.ctx
        if self.service is not None:
            for item in self.service.list_sessions():
                try:
                    self.service.close(item["id"])
                except Exception:
                    if ctx is not None:
                        ctx.logger.exception(
                            "Failed to close agent session %s", item["id"]
                        )
        self.service = None
