"""Ask plugin: in-note question answering with streaming and two intensities.

The plugin owns *questions*, not the agent loop: the ``llm-agent`` plugin runs
the model, builds the tools from the declarations below and streams deltas
back.  Answers live in the note as ``qa`` markdown blocks (owned by ``reader``).
"""

from __future__ import annotations

import json
import re
import threading
import time
import tempfile
from pathlib import Path
from typing import Any, Mapping

from core.agent_types import AgentProfile, AgentTool
from core.plugin import Plugin, PluginContext
from core.settings_spec import SettingsSpec
from core.ui import render_markdown
from core.file_access import publication_lock
from core.workspace import FileWorkspace

from .prompts import ASK_PROMPT_TEMPLATE, INSTANT_SYSTEM_PROMPT, RESEARCH_SYSTEM_PROMPT

ACTION = "ask.ask"
SELECTION_ID = "ask.ask"

INSTANT = "instant"
RESEARCH = "research"


class AskService:
    """Narrow public ``ask`` service (1 method)."""

    __slots__ = ("_plugin",)

    def __init__(self, plugin: "AskPlugin") -> None:
        self._plugin = plugin

    def ask(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ask one question (same payload as the ``ask.ask`` UI action)."""

        return self._plugin._on_ask(payload)  # noqa: SLF001 - the plugin owns it


class AskPlugin(Plugin):
    """Reader selection menu → in-note Q&A with a live, collapsible answer."""

    id = "ask"
    requires = ("settings", "llm-agent", "reader")
    optional_requires = ("summarize", "instant-summarize")
    provides = ("ask",)

    def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._settings = ctx.require("settings")
        self._agent = ctx.require("agent")
        self._reader = ctx.require("reader")
        self._active: set[str] = set()
        self._active_lock = threading.Lock()
        self._closed = False

        for spec in _SETTING_SPECS:
            self._settings.register(spec)

        ctx.register_service("ask", AskService(self))
        ctx.ui.register_action(ACTION, self._on_ask, plugin_id=self.id)

        self._reader.register_selection_action(
            "",
            {
                "id": SELECTION_ID,
                "label": "提问",
                "icon": "💬",
                "kind": "prompt",
                "action": ACTION,
                "placeholder": "针对选中的内容提问，Enter 发送",
                "intensities": [
                    {"value": INSTANT, "label": "快速"},
                    {"value": RESEARCH, "label": "研究"},
                ],
                "order": 10,
            },
            owner=self.id,
        )

    def teardown(self) -> None:
        """Drop the UI action and the selection-menu item this plugin added."""

        with self._active_lock:
            self._closed = True
            for session_id in tuple(self._active):
                self._agent.cancel(session_id)
        ctx = getattr(self, "ctx", None)
        if ctx is not None:
            try:
                ctx.ui.unregister_action(ACTION)
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass
        reader = getattr(self, "_reader", None)
        if reader is not None:
            try:
                reader.unregister_selection_action("", SELECTION_ID, owner=self.id)
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def _setting(self, key: str, default: Any) -> Any:
        return self._settings.get(f"ask.{key}", default)

    @property
    def _before_words(self) -> int:
        return max(0, int(self._setting("before_words", 2000)))

    @property
    def _after_words(self) -> int:
        return max(0, int(self._setting("after_words", 2000)))

    @property
    def _history_words(self) -> int:
        return max(0, int(self._setting("history_words", 1000)))

    @property
    def _selection_words(self) -> int:
        return max(50, int(self._setting("selection_words", 800)))

    @property
    def _instant_prompt(self) -> str:
        return str(
            self._setting("instant_prompt", INSTANT_SYSTEM_PROMPT)
            or INSTANT_SYSTEM_PROMPT
        )

    @property
    def _research_prompt(self) -> str:
        return str(
            self._setting("research_prompt", RESEARCH_SYSTEM_PROMPT)
            or RESEARCH_SYSTEM_PROMPT
        )

    # ------------------------------------------------------------------
    # Action
    # ------------------------------------------------------------------
    def _on_ask(self, payload: dict[str, Any]) -> dict[str, Any]:
        question = str(payload.get("question", "") or "").strip()
        selection = str(payload.get("selection", "") or "").strip()
        intensity = str(payload.get("intensity", "") or INSTANT)
        if intensity not in (INSTANT, RESEARCH):
            intensity = INSTANT
        path = str(payload.get("path", "") or "")
        if not question:
            return {"ok": False, "message": "问题不能为空"}

        created = self._reader.qa_begin(
            path,
            question=question,
            selection=selection,
            intensity=intensity,
        )
        qa_id = str(created.get("id", "") or "")
        threading.Thread(
            target=self._run,
            args=(path, qa_id, selection, intensity),
            name=f"ask-{qa_id}",
            daemon=True,
        ).start()

        message = "已提交，Agent 正在回答…"
        if not created.get("anchored", True):
            message += "（未能定位选中片段，已附在笔记末尾）"
        return {"ok": True, "message": message, "refresh": True}

    def _run(self, path: str, qa_id: str, selection: str, intensity: str) -> None:
        live = _Live(self, path, qa_id)
        status = "done"
        answer = ""
        started = time.time()
        try:
            answer = self._answer(path, qa_id, selection, intensity, live)
        except Exception as exc:  # noqa: BLE001 - the failure belongs in the note
            self.ctx.logger.exception("ask %s failed", qa_id)
            status = "error"
            answer = f"（回答失败：{exc}）"

        self._reader.qa_finish(
            path,
            qa_id,
            answer=answer,
            status=status,
            collapsed=(status == "done"),
        )
        live.finish(status=status, answer=answer, elapsed=time.time() - started)

    def _answer(
        self,
        path: str,
        qa_id: str,
        selection: str,
        intensity: str,
        live: "_Live",
    ) -> str:
        context = self._reader.qa_context(
            path,
            qa_id,
            before_words=self._before_words,
            after_words=self._after_words,
            history_words=self._history_words,
        )
        prompt = self._build_prompt(path, context, selection, intensity)
        files = self._workspace_files(path) if intensity == RESEARCH else {}
        started = time.time()
        with tempfile.TemporaryDirectory(prefix="notes-ask-") as snapshot:
            frozen = {}
            with publication_lock(Path(path).parent):
                for name, original in files.items():
                    destination = Path(snapshot) / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(Path(original).read_bytes())
                    frozen[name] = destination
            profile = self._profile(intensity)
            with self._active_lock:
                if self._closed:
                    raise RuntimeError("Ask service is closed")
                session = self._agent.create_session(
                    name=f"ask-{qa_id}",
                    agent=profile,
                    workspace=FileWorkspace(frozen, readonly=frozen),
                    meta={
                        "internal": True,
                        "role": "ask",
                        "qa_id": qa_id,
                        "intensity": intensity,
                    },
                )
                self._active.add(session.id)
            try:
                result = self._agent.run(session.id, prompt, on_event=live.on_event)
            finally:
                try:
                    self._agent.close(session.id)
                finally:
                    with self._active_lock:
                        self._active.discard(session.id)

        self.ctx.logger.info(
            "ask %s finished in %.1fs: intensity=%s steps=%s tokens=%s error=%s",
            qa_id,
            time.time() - started,
            intensity,
            result.steps,
            result.total_tokens,
            result.error,
        )
        if result.error:
            raise RuntimeError(result.error)
        return result.final_message.strip() or "（模型没有返回内容）"

    def _profile(self, intensity: str) -> AgentProfile:
        return AgentProfile(
            name=f"ask.{intensity}",
            role="ask_instant" if intensity == INSTANT else "ask_research",
            system_prompt=self._instant_prompt
            if intensity == INSTANT
            else self._research_prompt,
            tools=[]
            if intensity == INSTANT
            else [AgentTool(name) for name in ("read", "grep", "glob")],
        )

    def _build_prompt(
        self,
        path: str,
        context: Mapping[str, Any],
        selection: str,
        intensity: str,
    ) -> str:
        name = Path(path).name
        if intensity == RESEARCH:
            readable = "、".join(f"`{item}`" for item in self._workspace_files(path))
            instructions = (
                f"可用 read、grep、glob 核对完整授权材料．可读文件：{readable}．"
            )
        else:
            instructions = "材料已经足够，直接回答；本题没有工具可用。"
        return ASK_PROMPT_TEMPLATE.format(
            course_name=self._course_name(),
            file_label=self._doc_label(name),
            file_name=name,
            selection=_truncate_words(selection, self._selection_words)
            or "（浏览器没有回传选中文本）",
            before_words=self._before_words,
            before=context.get("before") or "（无）",
            after_words=self._after_words,
            after=context.get("after") or "（无）",
            history_words=self._history_words,
            history=_format_history(context.get("history") or []),
            question=context.get("question") or "",
            instructions=instructions,
        )

    def _workspace_files(self, path: str) -> dict[str, str]:
        note = Path(path)
        names = set()
        names.add(note.name)
        for owner in ("summarize", "instant-summarize"):
            service = self.ctx.service(owner)
            if service is not None:
                names.update(name for name, _ in service.note_files().values())
        files = {
            name: str(note.parent / name)
            for name in sorted(names)
            if (note.parent / name).is_file()
        }
        source = self._source_file(note)
        if source is not None:
            files[source.name] = str(source)
        return files

    def _source_file(self, note: Path) -> Path | None:
        """The lecture record that belongs to *note*, asked from its owner."""

        service = self.ctx.service("summarize")
        if service is not None:
            for session_id in (note.parent.parent.name,):
                if not session_id:
                    continue
                found = service.source_path(session_id)
                if found is not None and Path(found).exists():
                    return Path(found)
        fallback = note.parent.parent / "lecture_source.md"
        return fallback if fallback.exists() else None

    def _doc_label(self, name: str) -> str:
        """Human title of a note file, asked from the plugin that writes it."""

        for service_name in ("summarize", "instant-summarize"):
            service = self.ctx.service(service_name)
            if service is None:
                continue
            try:
                files = service.note_files()
            except Exception:  # noqa: BLE001 - a label must never break an answer
                continue
            for filename, title in (files or {}).values():
                if filename == name:
                    return title
        return _DEFAULT_DOC_LABELS.get(name, Path(name).stem)

    def _course_name(self) -> str:
        meta = self.ctx.streams.notes_meta or self.ctx.streams.current.meta or {}
        name: Any = meta.get("course_name") or meta.get("course")
        if isinstance(name, dict):
            name = name.get("course_name") or name.get("name") or ""
        return str(name or "").strip() or "未知课程"


# ---------------------------------------------------------------------------
# Live streaming helper
# ---------------------------------------------------------------------------
class _Live:
    """Accumulates the agent's stream and pushes throttled UI notifications."""

    _INTERVAL = 0.1

    def __init__(self, plugin: AskPlugin, path: str, qa_id: str) -> None:
        self._plugin = plugin
        self._path = path
        self._qa_id = qa_id
        self._phase = "thinking"
        self._tools: list[dict[str, Any]] = []
        self._reasoning = ""
        self._answer = ""
        self._pending_reasoning = ""
        self._pending_text = ""
        self._started = time.time()
        self._last = 0.0
        self._lock = threading.Lock()

    def on_event(self, event: Any) -> None:
        kind = event.type
        data = event.data or {}
        step = event.step
        logger = self._plugin.ctx.logger

        if kind == "agent.reasoning":
            delta = str(data.get("text") or "")
            if not delta:
                return
            with self._lock:
                self._reasoning += delta
                self._pending_reasoning += delta
                self._phase = "thinking"
            self.publish()
        elif kind == "agent.delta":
            delta = str(data.get("text") or "")
            if not delta:
                return
            with self._lock:
                self._answer += delta
                self._pending_text += delta
                self._phase = "answering"
            self.publish()
        elif kind == "agent.tool_call":
            name = str(data.get("name") or "?")
            arguments = data.get("arguments") or {}
            logger.info(
                "ask %s step %s tool %s %s",
                self._qa_id,
                step,
                name,
                json.dumps(arguments, ensure_ascii=False),
            )
            with self._lock:
                self._tools.append(
                    {
                        "name": name,
                        "id": data.get("id", data.get("tool_call_id")),
                        "detail": _tool_detail(name, arguments),
                        "done": False,
                    }
                )
                self._phase = "tool"
            self.publish(force=True)
        elif kind == "agent.tool_result":
            logger.info(
                "ask %s step %s tool %s result: %s",
                self._qa_id,
                step,
                data.get("name", "?"),
                str(data.get("result", ""))[:500],
            )
            with self._lock:
                call_id = data.get("id", data.get("tool_call_id"))
                for tool in self._tools:
                    if not tool["done"] and (
                        call_id is None or tool.get("id") == call_id
                    ):
                        tool["done"] = True
                        break
            self.publish(force=True)
        elif kind == "agent.message":
            content = str(data.get("content") or "")
            if content:
                logger.info("ask %s step %s reply:\n%s", self._qa_id, step, content)
        elif kind == "agent.error":
            logger.warning(
                "ask %s step %s error: %s", self._qa_id, step, data.get("error")
            )

    def publish(self, *, force: bool = False) -> None:
        with self._lock:
            now = time.time()
            if not force and now - self._last < self._INTERVAL:
                return
            self._last = now
            reasoning = self._pending_reasoning
            text = self._pending_text
            self._pending_reasoning = ""
            self._pending_text = ""
            payload = {
                "path": self._path,
                "id": self._qa_id,
                "status": "pending",
                "phase": self._phase,
                "reasoning_delta": reasoning,
                "text_delta": text,
                "tools": [dict(tool) for tool in self._tools],
                "elapsed": round(now - self._started, 1),
            }
        self._plugin.ctx.ui.notify("qa.live", payload)

    def finish(self, *, status: str, answer: str, elapsed: float) -> None:
        payload = {
            "path": self._path,
            "id": self._qa_id,
            "status": status,
            "phase": "done" if status == "done" else "error",
            "answer": answer,
            "answer_html": render_markdown(answer),
            "collapsed": status == "done",
            "elapsed": round(elapsed, 1),
        }
        logger = self._plugin.ctx.logger
        # The full thinking trace goes to the log verbatim, never truncated.
        if self._reasoning:
            logger.info(
                "ask %s reasoning (%d chars):\n%s",
                self._qa_id,
                len(self._reasoning),
                self._reasoning,
            )
        logger.info(
            "ask %s answer (%s, %.1fs):\n%s",
            self._qa_id,
            status,
            elapsed,
            answer,
        )
        self._plugin.ctx.ui.notify("qa.live", payload)


def _tool_detail(name: str, arguments: Mapping[str, Any]) -> str:
    if name == "read":
        return str(arguments.get("path", "?"))
    if name == "grep":
        return f"{arguments.get('pattern', '')} @ {arguments.get('path', '*')}"
    if name == "glob":
        return str(arguments.get("pattern", arguments.get("path", "*")))
    return json.dumps(arguments, ensure_ascii=False)[:120]


# Fallback titles when the notes plugins are not loaded (service API first).
_DEFAULT_DOC_LABELS = {
    "note_knowledge.md": "知识",
    "note_gossip.md": "闲话与分享",
    "note_notice.md": "通知",
    "instant_note_knowledge.md": "即时知识",
    "instant_note_gossip.md": "即时闲话",
    "instant_note_notice.md": "即时通知",
}


_WORD_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]|[A-Za-z]+|[0-9]+")


def _truncate_words(text: str, limit: int) -> str:
    text = (text or "").strip()
    if limit <= 0 or not text:
        return text
    matches = list(_WORD_RE.finditer(text))
    if len(matches) <= limit:
        return text
    return text[: matches[limit - 1].end()] + "\n……（选中内容过长，已截断）"


def _format_history(entries: list[dict[str, str]]) -> str:
    if not entries:
        return "（无）"
    parts: list[str] = []
    for index, entry in enumerate(entries, 1):
        parts.append(
            f"### 问答 {index}\n\n问：{entry.get('question', '')}\n\n"
            f"答：{entry.get('answer', '')}"
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
_SETTING_SPECS: list[SettingsSpec] = [
    SettingsSpec(
        key="ask.before_words",
        label="选中前文词数",
        type="int",
        default=2000,
        group="提问",
        description="上下文里选中段落之前的笔记内容（按词计，中文按字计）",
    ),
    SettingsSpec(
        key="ask.after_words",
        label="选中后文词数",
        type="int",
        default=2000,
        group="提问",
        description="上下文里选中段落之后的笔记内容",
    ),
    SettingsSpec(
        key="ask.history_words",
        label="历史问答词数",
        type="int",
        default=1000,
        group="提问",
        description="同一条笔记中此前问答记录的独立限额",
    ),
    SettingsSpec(
        key="ask.selection_words",
        label="选中内容词数",
        type="int",
        default=800,
        group="提问",
        description="选中片段送入模型的上限，超出部分截断",
    ),
    SettingsSpec(
        key="ask.instant_prompt",
        label="快速模式提示词",
        type="text",
        default=INSTANT_SYSTEM_PROMPT,
        group="提问",
        description="快速模式（无工具、一步回答）的系统提示词",
    ),
    SettingsSpec(
        key="ask.research_prompt",
        label="研究模式提示词",
        type="text",
        default=RESEARCH_SYSTEM_PROMPT,
        group="提问",
        description="研究模式只读答疑的系统提示词",
    ),
]


__all__ = ["AskPlugin", "ACTION"]
