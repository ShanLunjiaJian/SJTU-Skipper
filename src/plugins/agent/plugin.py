"""Manage application sessions and forward SDK events to the UI."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from core.file_access import publication_lock
from core.agent_types import AgentEvent, AgentProfile, AgentRunResult, AgentTool
from core.plugin import Plugin, PluginContext
from core.settings_spec import SettingsSpec

from .prompts import DEFAULT_SYSTEM_PROMPT
from .session import AgentSession
from .workspace import FileWorkspace

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
        self._runtime = ctx.llm.runtime
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
            role=role or (profile.role if profile else "agent_chat"),
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
        session.model = str(result.get("model") or session.role)
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

    def reset(self, session_id: str) -> None:
        session = self._require_session(session_id)
        self.cancel(session_id)
        with session.lock:
            if session.runtime_ready:
                self._runtime.call("reset", session_id=session_id)
            else:
                self._create_runtime_session(session)
            session.messages.clear()
            session.cancelled.clear()

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
            session.messages.append({"role": "user", "content": user_input})
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
                if name == "agent.message":
                    session.messages.append(
                        {"role": "assistant", "content": str(data.get("content") or "")}
                    )
                elif name == "agent.tool_call":
                    calls += 1
                elif name == "agent.tool_result":
                    session.messages.append(
                        {
                            "role": "tool",
                            "content": str(data.get("result") or ""),
                            "name": data.get("name", ""),
                        }
                    )
                elif name == "agent.file":
                    logical = str(data.get("file") or "")
                    if logical not in session.workspace:
                        raise PermissionError(
                            f"Runtime changed an unauthorized file: {logical}"
                        )
                    path = session.workspace.path(logical)
                    lecture = session.meta.get("lecture_session")
                    publication = (
                        self._ctx.data_dir / "sessions" / str(lecture) / "notes"
                        if lecture
                        else path.parent
                    )
                    with publication_lock(publication):
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
            if result.final_message and (
                not session.messages
                or session.messages[-1].get("content") != result.final_message
            ):
                session.messages.append(
                    {"role": "assistant", "content": result.final_message}
                )
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

    def _run_async(self, session_id: str, user_input: str) -> threading.Thread:
        thread = threading.Thread(
            target=self.run,
            args=(session_id, user_input),
            daemon=True,
            name=f"agent-run-{session_id}",
        )
        thread.start()
        return thread

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

_AGENT_PAGE_ID = "agent:chat"


def _is_user_session(item: Any) -> bool:
    """True for sessions the user owns.

    Plugins that drive the agent in the background mark their sessions with
    ``meta["internal"] = True``; nothing here knows their names.
    """

    if isinstance(item, Mapping):
        return not bool(item.get("internal"))
    meta = getattr(item, "meta", None)
    return not bool(meta.get("internal")) if isinstance(meta, Mapping) else True


class AgentPlugin(Plugin):
    """Registers the ``agent`` service, tools, settings, and UI.

    id: ``"llm-agent"``
    requires: ``("settings", "llm")``
    """

    id = "llm-agent"
    requires = ("settings", "llm")
    provides = ("agent",)

    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.service: AgentService | None = None
        self._page_added = False
        self._command_added = False
        self._actions: list[str] = []
        self._current_session_id = ""

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
                settings.register(spec)
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
                "role": "agent_chat",
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

        # --- UI ---
        # The agent is a page, not a sidebar tab: open it from the menu bar or
        # the command palette (Ctrl+P).
        ui = ctx.ui
        ui.add_page(
            _AGENT_PAGE_ID,
            "Agent",
            render=self._render_chat,
            icon="✦",
            order=600,
            plugin_id=self.id,
        )
        self._page_added = True

        # Actions
        for action_id, handler in [
            ("agent.chat", self._handle_chat),
            ("agent.new_session", self._handle_new_session),
            ("agent.select_session", self._handle_select_session),
            ("agent.reset", self._handle_reset),
        ]:
            ui.register_action(action_id, handler, plugin_id=self.id)
            self._actions.append(action_id)

        ui.register_command(
            "agent.open",
            "Agent",
            subtitle="与课堂 Agent 对话、查看会话",
            category="视图",
            icon="✦",
            order=20,
            page_id=_AGENT_PAGE_ID,
            keywords="agent chat session llm",
            plugin_id=self.id,
        )
        self._command_added = True

        ctx.logger.info("agent plugin ready; %s sessions", 0)

    def teardown(self) -> None:
        ctx = self.ctx
        if ctx is not None:
            for action_id in self._actions:
                try:
                    ctx.ui.unregister_action(action_id)
                except Exception:
                    pass
            self._actions.clear()
            if self._command_added:
                try:
                    ctx.ui.unregister_command("agent.open")
                except Exception:
                    pass
                self._command_added = False
            if self._page_added:
                try:
                    ctx.ui.remove_page(_AGENT_PAGE_ID)
                except Exception:
                    pass
                self._page_added = False
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

    # ------------------------------------------------------------------
    # UI renderers
    # ------------------------------------------------------------------
    def _ui_sessions(self) -> list[dict[str, Any]]:
        """User-facing sessions; hide summarize's internal worker sessions."""

        service = self.service
        if service is None:
            return []
        return [s for s in service.list_sessions() if _is_user_session(s)]

    def _current_session(self, sessions: list[dict[str, Any]]) -> str:
        """Return the selected session id, falling back to the newest one."""

        ids = {s["id"] for s in sessions}
        if self._current_session_id in ids:
            return self._current_session_id
        preferred = next(
            (s for s in reversed(sessions) if _is_user_session(s)),
            sessions[-1] if sessions else None,
        )
        self._current_session_id = preferred["id"] if preferred else ""
        return self._current_session_id

    def _render_chat(self) -> list[dict[str, Any]]:
        service = self.service
        if service is None:
            return [{"type": "text", "text": "服务未就绪"}]
        sessions = self._ui_sessions()
        if not sessions:
            return [
                {
                    "type": "markdown",
                    "text": "### Agent\n\n暂无会话。请先创建会话。",
                }
            ]
        current_id = self._current_session(sessions)
        current = next((s for s in sessions if s["id"] == current_id), sessions[0])
        session = service.get_session(current["id"])
        blocks: list[dict[str, Any]] = [
            {
                "type": "markdown",
                "text": f"### Agent - {current['name']}\n\n"
                f"model: {current['model']}  |  "
                f"messages: {current['message_count']}",
            }
        ]
        if session is not None:
            # A running session holds its lock for the whole run (minutes for a
            # handout job), so waiting for it would freeze the entire UI state
            # render.  Take a best-effort snapshot instead.
            if session.lock.acquire(timeout=0.05):
                try:
                    msgs = list(session.messages)
                finally:
                    session.lock.release()
            else:
                msgs = list(session.messages)
                blocks.append(
                    {
                        "type": "text",
                        "text": "（会话正在运行，消息列表可能不完整）",
                    }
                )
            for msg in msgs[-20:]:
                role = msg.get("role", "?")
                content = str(msg.get("content", ""))
                if not content and role == "assistant":
                    content = "*(tool calls)*"
                if not content:
                    content = f"*(tool result for {msg.get('name', '?')})*"
                blocks.append(
                    {
                        "type": "log",
                        "lines": [
                            {
                                "time": role.upper(),
                                "text": content[:500],
                            }
                        ],
                    }
                )
        blocks.append(
            {
                "type": "form",
                "fields": [
                    {
                        "name": "message",
                        "label": "输入",
                        "type": "text",
                        "placeholder": "输入消息...",
                    }
                ],
                "submit": {"label": "发送", "action": "agent.chat"},
            }
        )
        return blocks

    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------
    def _handle_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        service = self.service
        if service is None:
            return {"ok": False, "message": "服务未就绪"}
        message = str(
            payload.get("message", "") or payload.get("values", {}).get("message", "")
        )
        if not message.strip():
            return {"ok": False, "message": "消息为空"}

        sessions = self._ui_sessions()
        if not sessions:
            session = service.create_session("default")
            self._current_session_id = session.id
            session_id = session.id
        else:
            session_id = self._current_session(sessions)

        service._run_async(session_id, message)  # noqa: SLF001 - internal UI path
        return {"ok": True, "message": "Agent 正在处理…", "refresh": True}

    def _handle_new_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        service = self.service
        if service is None:
            return {"ok": False, "message": "服务未就绪"}
        name = str(payload.get("name", "") or "default")
        session = service.create_session(name)
        self._current_session_id = session.id
        return {
            "ok": True,
            "message": f"已创建会话 {name!r}",
            "refresh": True,
            "open_page": _AGENT_PAGE_ID,
        }

    def _handle_select_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        service = self.service
        if service is None:
            return {"ok": False, "message": "服务未就绪"}
        values = (
            payload.get("values") if isinstance(payload.get("values"), dict) else {}
        )
        session_id = str(payload.get("session_id") or values.get("session_id") or "")
        if not session_id:
            return {"ok": False, "message": "缺少会话 ID"}
        if service.get_session(session_id) is None:
            return {"ok": False, "message": "会话不存在或已关闭"}
        self._current_session_id = session_id
        return {
            "ok": True,
            "message": "已切换会话",
            "refresh": True,
            "open_page": _AGENT_PAGE_ID,
        }

    def _handle_reset(self, payload: dict[str, Any]) -> dict[str, Any]:
        service = self.service
        if service is None:
            return {"ok": False, "message": "服务未就绪"}
        values = (
            payload.get("values") if isinstance(payload.get("values"), dict) else {}
        )
        session_id = str(payload.get("session_id") or values.get("session_id") or "")
        if not session_id:
            session_id = self._current_session(self._ui_sessions())
        if not session_id:
            return {"ok": False, "message": "没有可重置的会话"}
        service.reset(session_id)
        return {"ok": True, "message": "已重置", "refresh": True}
