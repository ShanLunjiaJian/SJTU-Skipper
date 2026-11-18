from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from core.coverage import Coverage, CoverageStore, skip_coverage
from core.plugin import Plugin, PluginContext
from core.media import MediaKind, ProcessingMode
from core.streams import StreamKind, TextSource
from core.ui import BlockType
from core.settings_spec import SettingType, SettingsSpec

from .client import STTClient, SttEventType, retryable
from .errors import SttError


class SttState(StrEnum):
    DISABLED = "disabled"
    ERROR = "error"
    RUNNING = "running"
    IDLE = "idle"


PAGE_ID = "stt:transcript"
TRANSCRIPT_LOG = "transcript.jsonl"
EPS = 0.5 / 16000


@dataclass
class SessionState:
    scheduled: float = 0.0
    sent: float = 0.0
    received: float = 0.0
    active: bool = False
    flushing: bool = False
    partial: dict[str, Any] | None = None
    failed: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    language: str = ""
    error: str = ""
    done: threading.Event = field(default_factory=threading.Event)


class STTService:
    __slots__ = ("_plugin",)

    def __init__(self, plugin: STTPlugin) -> None:
        self._plugin = plugin

    def status(self) -> dict[str, Any]:
        return self._plugin.status()

    def enabled(self) -> bool:
        return self._plugin.enabled()

    def busy(self) -> bool:
        return self._plugin.busy()

    def flushing(self) -> bool:
        return self._plugin.flushing()

    def transcribe(
        self, start: float | None = None, end: float | None = None
    ) -> dict[str, Any]:
        return self._plugin.transcribe_range(start, end)

    def transcript_path(self, session_id: str = "") -> Path | None:
        return self._plugin.transcript_path(session_id)


class STTPlugin(Plugin):
    """The service owns acoustic segmentation. Only final text is permanent."""

    id = "stt"
    requires = ("settings",)
    provides = ("stt",)

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._lock = threading.RLock()
        self._persist_lock = threading.Lock()
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._client: STTClient | None = None
        self._processing_session: Any = None
        self._states: dict[str, SessionState] = {}
        self._sessions: dict[str, Any] = {}
        self._disabled_at: dict[str, float] = {}
        self._coverage_cache: dict[str, CoverageStore] = {}
        self._page_added = False
        self._command_added = False
        self._consumer_registered = False
        self._logger = logging.getLogger("lecture_agent.plugin.stt")
        self._closing = False

    def setup(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._register_settings()
        ctx.register_service("stt", STTService(self))
        ctx.meta.register("stt", self._meta, plugin_id=self.id)
        ctx.ui.add_page(
            PAGE_ID,
            "语音转写",
            render=self._render_page,
            icon="mic",
            order=20,
            plugin_id=self.id,
        )
        self._page_added = True
        ctx.ui.register_command(
            "stt.debug",
            "语音转写（调试）",
            subtitle="查看临时文字、定稿与处理进度",
            category="调试",
            icon="🎙",
            order=20,
            page_id=PAGE_ID,
            keywords="stt transcript debug 转写",
            plugin_id=self.id,
        )
        self._command_added = True
        self._register_consumer(ctx)
        ctx.events.on("session.started", self._on_session_started, plugin_id=self.id)
        ctx.events.on("session.stopped", self._on_session_stopped, plugin_id=self.id)
        ctx.events.on("settings.changed", self._on_settings_changed, plugin_id=self.id)
        self._worker = threading.Thread(
            target=self._worker_loop, name="stt-worker", daemon=True
        )
        self._worker.start()

    def teardown(self) -> None:
        ctx = self._ctx
        self._closing = True
        self._stop.set()
        self._cancel_requests()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=3.0)
        if ctx is not None:
            if self._consumer_registered:
                try:
                    ctx.streams.unregister_consumer(self.id)
                except Exception:  # noqa: BLE001 - hub may already be gone
                    pass
                self._consumer_registered = False
            if self._page_added:
                try:
                    ctx.ui.remove_page(PAGE_ID)
                except Exception:  # noqa: BLE001 - UI may already be gone
                    pass
                self._page_added = False
            if self._command_added:
                try:
                    ctx.ui.unregister_command("stt.debug")
                except Exception:  # noqa: BLE001
                    pass
                self._command_added = False
        if worker is None or not worker.is_alive():
            self._ctx = None

    def _register_consumer(self, ctx: PluginContext) -> None:

        if ctx.streams is None:
            return
        ctx.streams.register_consumer(
            self.id,
            MediaKind.AUDIO,
            cursor_fn=lambda: self._watermark(ctx.streams.current),
            busy_fn=lambda: self.busy() or self.flushing(),
            gate_limit_fn=self._gate_limit,
        )
        self._consumer_registered = True

    def _gate_limit(self) -> float:
        """Return the local unprocessed audio limit in seconds."""
        if not self.enabled():
            return 0.0
        return max(0.2, float(self._setting("stt.buffer_seconds", 240.0)))

    def _register_settings(self) -> None:
        settings = self._ctx.service("settings")
        for suffix in (
            "credentials_json",
            "credentials_file",
            "project_id",
            "location",
            "model",
            "language_code",
            "enable_punctuation",
            "chunk_seconds",
            "api_key",
        ):
            settings.reset("stt." + suffix)
        entries = (
            ("enabled", "启用语音转写", SettingType.BOOL, True, ""),
            (
                "server_url",
                "服务地址",
                SettingType.STRING,
                "http://127.0.0.1:8766",
                "兼容 STT HTTP API 的服务地址",
            ),
            ("language", "语言", SettingType.STRING, "Chinese", "服务使用的转写语言"),
            ("context", "转写提示", SettingType.TEXT, "", "留空使用无提示转写"),
            (
                "timeout",
                "请求超时（秒）",
                SettingType.FLOAT,
                60.0,
                "单次请求的超时时间",
            ),
            (
                "send_window_seconds",
                "发送确认窗口（秒）",
                SettingType.FLOAT,
                120.0,
                "录播已发送但未确认接收的音频上限；服务负责语音分段",
            ),
            (
                "buffer_seconds",
                "本地音频缓冲（秒）",
                SettingType.FLOAT,
                240.0,
                "暂停读取前允许保留的未处理音频时长",
            ),
            (
                "concurrency",
                "服务端录播并发数",
                SettingType.INT,
                1,
                "请求的活动推理数，由服务端限制到可用容量",
            ),
        )
        for order, (suffix, label, kind, default, description) in enumerate(entries):
            settings.register(
                SettingsSpec(
                    key="stt." + suffix,
                    label=label,
                    type=kind,
                    default=default,
                    group="STT",
                    description=description,
                    order=order,
                    env=("STT_SERVER_URL",) if suffix == "server_url" else (),
                )
            )

    def _setting(self, key: str, default: Any = None) -> Any:
        settings = self._ctx.service("settings") if self._ctx is not None else None
        return settings.get(key, default) if settings is not None else default

    def enabled(self) -> bool:
        return bool(self._setting("stt.enabled", True))

    def _state(self, session: Any) -> SessionState:
        with self._lock:
            return self._states.setdefault(session.session_id, SessionState())

    def _coverage(self, session: Any) -> CoverageStore:
        with self._lock:
            sid = session.session_id
            if sid not in self._coverage_cache:
                self._coverage_cache[sid] = CoverageStore(self._ctx.data_dir, sid)
            return self._coverage_cache[sid]

    def _covered(self, session: Any) -> Coverage:
        return Coverage(
            [
                *self._coverage(session).coverage(MediaKind.AUDIO).intervals,
                *skip_coverage(session.meta, MediaKind.AUDIO).intervals,
            ]
        )

    def _watermark(self, session: Any) -> float:
        intervals = self._covered(session).intervals
        return intervals[0][1] if intervals and intervals[0][0] <= EPS else 0.0

    def busy(self) -> bool:
        if self._ctx is None or not self.enabled():
            return False
        session = self._ctx.streams.current
        state = self._state(session)
        return (
            state.active
            or state.partial is not None
            or state.scheduled + EPS < session.audio.duration
        )

    def flushing(self) -> bool:
        return bool(
            self._ctx is not None and self._state(self._ctx.streams.current).flushing
        )

    def status(self) -> dict[str, Any]:
        if self._ctx is None:
            return {"state": SttState.DISABLED, "cursor": 0.0}
        session = self._ctx.streams.current
        state = self._state(session)
        items = [
            item
            for item in session.transcript.items_since(0)
            if item.source == TextSource.STT
        ]
        covered = self._covered(session).total()
        return {
            "state": SttState.DISABLED
            if not self.enabled()
            else SttState.ERROR
            if state.error
            else SttState.RUNNING
            if state.active
            else SttState.IDLE,
            "backend": "http-stt",
            "server_url": self._setting("stt.server_url"),
            "error": state.error,
            "model": state.model,
            "language": state.language,
            "session_id": session.session_id,
            "audio_seconds": session.audio.duration,
            "cursor": self._watermark(session),
            "sent_cursor": state.sent,
            "received_cursor": state.received,
            "scheduled_cursor": state.scheduled,
            "covered_seconds": covered,
            "uncovered_seconds": max(0.0, session.audio.duration - covered),
            "segments": len(items),
            "chars": sum(len(item.text) for item in items),
            "partial": state.partial,
            "failed_ranges": list(state.failed),
            "anomalies": list(state.anomalies),
            "busy": self.busy(),
            "flushing": state.flushing,
            "worker_alive": self._worker is not None and self._worker.is_alive(),
        }

    def _meta(self) -> dict[str, Any]:
        status = self.status()
        return {
            key: status.get(key)
            for key in (
                "backend",
                "state",
                "server_url",
                "model",
                "language",
                "session_id",
                "audio_seconds",
                "segments",
                "chars",
            )
        }

    def _new_client(self) -> STTClient:
        return STTClient(
            str(self._setting("stt.server_url", "http://127.0.0.1:8766")),
            timeout=float(self._setting("stt.timeout", 60.0)),
        )

    def _metadata(self) -> dict[str, str]:
        return {
            "language": str(self._setting("stt.language", "Chinese")),
            "context": str(self._setting("stt.context", "")),
        }

    def _cancel_requests(self) -> None:
        self._cancel.set()
        with self._lock:
            client = self._client
        if client is not None:
            client.close()

    def _worker_loop(self) -> None:
        try:
            while not self._stop.is_set():
                if not self.enabled():
                    self._stop.wait(0.05)
                    continue
                with self._lock:
                    sessions = list(self._sessions.values())
                candidates = [
                    session
                    for session in sessions
                    if not self._ctx.streams.text_progress(
                        session.session_id, StreamKind.TRANSCRIPT
                    ).final
                    and (
                        session.audio.duration > self._state(session).scheduled + EPS
                        or self._finished(session)
                    )
                ]
                session = candidates[0] if candidates else self._ctx.streams.current
                self._sessions[session.session_id] = session
                state = self._state(session)
                if state.scheduled >= session.audio.duration - EPS:
                    if (
                        self._finished(session)
                        and not self._ctx.streams.text_progress(
                            session.session_id, StreamKind.TRANSCRIPT
                        ).final
                    ):
                        self._ctx.streams.seal_text(
                            session.session_id,
                            StreamKind.TRANSCRIPT,
                            state.scheduled,
                            final=True,
                        )
                        state.done.set()
                    session.audio.wait_for(
                        session.audio.duration + 1 / 16000, timeout=0.05
                    )
                    continue
                self._cancel = threading.Event()
                if not self.enabled():
                    continue
                client = self._new_client()
                with self._lock:
                    self._client = client
                    self._processing_session = session
                state.active = True
                state.done.clear()
                try:
                    self._run_live(session, client)
                except Exception as exc:
                    if not self._cancelled(session):
                        self._fail(
                            session, state.scheduled, session.audio.duration, exc
                        )
                        state.scheduled = session.audio.duration
                finally:
                    client.close()
                    with self._lock:
                        if self._client is client:
                            self._client = None
                        if self._processing_session is session:
                            self._processing_session = None
                    state.active = False
                    state.flushing = False
                    state.partial = None
                    state.done.set()
        finally:
            if self._closing:
                self._ctx = None

    def _cancelled(self, session: Any) -> bool:
        return self._stop.is_set() or self._cancel.is_set() or not self.enabled()

    @staticmethod
    def _finished(session: Any) -> bool:
        return (
            session.audio_input_done
            or session.input_done
            or session.ended_at is not None
        )

    def _next_interval(
        self, session: Any, start: float, end: float
    ) -> tuple[float, float] | None:
        for lo, hi in session.audio.buffered_ranges(start, end):
            gaps = self._covered(session).gaps(lo, hi)
            if gaps:
                return gaps[0]
        return None

    def _skip(self, session: Any, end: float) -> None:
        state = self._state(session)
        state.scheduled = max(state.scheduled, end)
        session.audio_gate.release(end)
        self._trim(session, end)

    def _trim(self, session: Any, end: float) -> None:
        earliest = session.audio.earliest_time
        for lo, hi in self._covered(session).intervals:
            if lo <= earliest + EPS <= hi:
                session.audio.trim(max(0.0, min(end, hi) - 1.0))
                return

    def _run_live(self, session: Any, client: STTClient) -> None:
        state, attempts = self._state(session), 0
        while not self._cancelled(session):
            available = session.audio.duration
            if self._finished(session) and state.scheduled >= available - EPS:
                state.scheduled = max(state.scheduled, available)
                return
            interval = self._next_interval(session, state.scheduled, available)
            if interval is None:
                self._skip(session, available)
                if self._finished(session):
                    return
                session.audio.wait_for(available + 1 / 16000, timeout=0.05)
                continue
            start, _ = interval
            if start > state.scheduled + EPS:
                self._skip(session, start)
            try:
                self._live_connection(session, client, start)
                attempts = 0
            except Exception as exc:
                if self._cancelled(session):
                    return
                attempts += 1
                state.scheduled = self._successful_end(session, start)
                if attempts < 3 and retryable(exc):
                    if self._cancel.wait(0.25 * 2 ** (attempts - 1)):
                        return
                    continue
                end = max(state.scheduled, state.sent, session.audio.duration)
                self._fail(session, state.scheduled, end, exc)
                state.scheduled = end
                session.audio_gate.release(end)
                attempts = 0
                if end <= start + EPS:
                    return

    def _successful_end(self, session: Any, start: float) -> float:
        for lo, hi in self._covered(session).intervals:
            if lo <= start + EPS < hi:
                return hi
        return start

    def _live_connection(self, session: Any, client: STTClient, start: float) -> None:
        state = self._state(session)
        offline = session.meta.get("mode") == ProcessingMode.FAST
        stream = client.stream(
            offline=offline,
            concurrency=max(1, int(self._setting("stt.concurrency", 1))),
            **self._metadata(),
        )
        state.model = str(stream.metadata.get("model", ""))
        state.language = str(stream.metadata.get("language", ""))
        done = threading.Event()
        errors: list[BaseException] = []
        connection = {
            "sent": start,
            "received": start,
            "processed": start,
            "activity": time.monotonic(),
        }
        # Offset and segment IDs belong only to this connection.
        try:
            stream.reset(round(start * 1000))
        except BaseException:
            client.close_stream(stream)
            raise

        def receive() -> None:
            try:
                while not done.is_set() and not self._cancelled(session):
                    try:
                        event = stream.receive(timeout=0.1)
                    except TimeoutError:
                        continue
                    self._live_event(session, event, start, connection)
                    connection["activity"] = time.monotonic()
                    if event["type"] == SttEventType.DONE:
                        done.set()
            except BaseException as exc:
                errors.append(exc)
                done.set()

        receiver = threading.Thread(target=receive, name="stt-events", daemon=True)
        receiver.start()
        sent = start
        try:
            while not self._cancelled(session) and not done.is_set():
                available = session.audio.duration
                ranges = session.audio.buffered_ranges(sent, available)
                hi = ranges[0][1] if ranges and ranges[0][0] <= sent + EPS else sent
                gaps = self._covered(session).gaps(sent, hi)
                hi = gaps[0][1] if gaps and gaps[0][0] <= sent + EPS else sent
                end = min(hi, sent + 0.2)
                credit = (
                    min(
                        120.0,
                        max(
                            0.2, float(self._setting("stt.send_window_seconds", 120.0))
                        ),
                    )
                    if offline
                    else 1.0
                )
                if end > connection["received"] + credit + EPS:
                    if time.monotonic() - connection["activity"] > float(
                        self._setting("stt.timeout", 60.0)
                    ):
                        raise SttError(
                            "The STT service stopped accepting audio.",
                            connection_failed=True,
                        )
                    done.wait(0.01)
                    continue
                if end > sent + EPS:
                    connection["sent"] = end
                    stream.send(session.audio.read(sent, end))
                    sent = end
                    state.sent = max(state.sent, sent)
                    continue
                if sent < available - EPS or self._finished(session):
                    state.flushing = True
                    stream.finish()
                    deadline = time.monotonic() + float(
                        self._setting("stt.timeout", 60.0)
                    )
                    while not done.wait(0.05) and not self._cancelled(session):
                        if time.monotonic() >= deadline:
                            raise SttError("The STT service did not finish the stream.")
                    break
                session.audio.wait_for(sent + 1 / 16000, timeout=0.05)
            if errors:
                raise errors[0]
            if not self._cancelled(session) and connection["processed"] < sent - 0.002:
                raise SttError(
                    "The STT service did not process the complete stream.",
                    connection_failed=True,
                )
            state.scheduled = max(state.scheduled, connection["processed"])
        finally:
            done.set()
            client.close_stream(stream)
            receiver.join(timeout=1.5)
            state.partial = None

    def _live_event(
        self,
        session: Any,
        event: dict[str, Any],
        start: float,
        connection: dict[str, float],
    ) -> None:
        if self._cancelled(session):
            return
        state = self._state(session)
        kind = SttEventType(event["type"])
        if kind == SttEventType.ACCEPTED:
            end = min(connection["sent"], float(event["received_ms"]) / 1000)
            state.received = max(state.received, end)
            connection["received"] = max(connection["received"], end)
        elif kind == SttEventType.PARTIAL:
            state.partial = dict(event)
            self._ctx.events.emit(
                "stt.partial",
                session_id=session.session_id,
                **{k: v for k, v in event.items() if k != "type"},
            )
            self._ctx.ui.refresh("stt.partial")
        elif kind == SttEventType.ANOMALY:
            self._anomaly(session, dict(event))
        elif kind == SttEventType.FINAL:
            lo, hi = (
                max(start, float(event["start_ms"]) / 1000),
                min(connection["sent"], float(event["end_ms"]) / 1000),
            )
            if state.partial is not None and state.partial.get(
                "segment_id"
            ) == event.get("segment_id"):
                state.partial = None
            if hi > lo and not self._covered(session).covers(lo, hi):
                self._append(session, lo, hi, str(event["text"]), {})
                self._success(session, lo, hi)
        elif kind in (SttEventType.PROGRESS, SttEventType.DONE):
            end = min(connection["sent"], float(event["processed_ms"]) / 1000)
            if end >= connection["processed"]:
                self._success(session, connection["processed"], end)
                connection["processed"] = end
                state.scheduled = max(state.scheduled, end)

    def _success(self, session: Any, start: float, end: float) -> None:
        self._coverage(session).mark(MediaKind.AUDIO, start, end)
        self._state(session).error = ""
        session.audio_gate.release(end)
        self._trim(session, end)
        self._ctx.streams.seal_text(session.session_id, StreamKind.TRANSCRIPT, end)

    def _append(
        self, session: Any, start: float, end: float, text: str, extra: dict[str, Any]
    ) -> Any:
        text = text.strip()
        if not text:
            return
        state = self._state(session)
        meta = {
            k: v for k, v in (("model", state.model), ("language", state.language)) if v
        }
        meta.update(extra)
        item = session.transcript.append(
            text, start=start, end=end, source=TextSource.STT, meta=meta
        )
        if item is None:
            return
        self._persist_item(session.session_id, item)
        if "anomaly" in extra:
            self._anomaly(
                session, {"code": extra["anomaly"], "start": start, "end": end}
            )
        self._ctx.events.emit(
            "stt.segment",
            session_id=session.session_id,
            start=start,
            end=end,
            segments=1,
            chars=len(text),
            **meta,
        )
        self._ctx.ui.refresh("stt.segment")
        return item

    def _anomaly(self, session: Any, event: dict[str, Any]) -> None:
        self._state(session).anomalies.append(event)
        self._ctx.events.emit(
            "stt.anomaly", session_id=session.session_id, anomaly=event
        )

    def _fail(
        self, session: Any, start: float, end: float, error: BaseException
    ) -> None:
        state = self._state(session)
        state.error = str(error)
        state.failed.append({"start": start, "end": end, "error": str(error)})
        self._ctx.streams.seal_text(
            session.session_id,
            StreamKind.TRANSCRIPT,
            end,
            error={"start": start, "end": end, "error": str(error)},
        )
        self._ctx.events.emit(
            "stt.error",
            session_id=session.session_id,
            start=start,
            end=end,
            error=str(error),
        )
        self._logger.warning("STT range %.3f-%.3f failed: %s", start, end, error)

    def transcribe_range(
        self, start: float | None = None, end: float | None = None
    ) -> dict[str, Any]:
        if self._ctx is None:
            raise SttError("The STT plugin is not initialized.")
        session = self._ctx.streams.current
        lo = self._state(session).scheduled if start is None else max(0.0, float(start))
        hi = min(session.audio.duration, lo + 120.0 if end is None else float(end))
        client = self._new_client()
        items: list[dict[str, Any]] = []
        try:
            for begin, finish in session.audio.buffered_ranges(lo, hi):
                for a, b in self._covered(session).gaps(begin, finish):
                    result = client.transcribe(
                        session.audio.read_as_wav(a, b), **self._metadata()
                    )
                    if self._stop.is_set() or self._ctx.streams.current is not session:
                        raise SttError("The STT request was cancelled.")
                    if abs(float(result["duration_ms"]) / 1000 - (b - a)) > 0.002:
                        raise SttError(
                            "The STT service did not process the complete upload."
                        )
                    state = self._state(session)
                    state.model, state.language = (
                        str(result.get("model", "")),
                        str(result.get("language", "")),
                    )
                    for segment in result["segments"]:
                        item = self._append(
                            session,
                            a + float(segment["start_ms"]) / 1000,
                            a + float(segment["end_ms"]) / 1000,
                            str(segment["text"]),
                            {k: segment[k] for k in ("anomaly",) if k in segment},
                        )
                        if item is not None:
                            items.append(
                                {
                                    "start": round(item.start, 3),
                                    "end": round(item.end, 3),
                                    "text": item.text,
                                }
                            )
                    self._success(session, a, b)
                    state.scheduled = max(state.scheduled, b)
        finally:
            client.close()
        return {
            "ok": True,
            "start": lo,
            "end": hi,
            "segments": len(items),
            "chars": sum(len(item["text"]) for item in items),
            "items": items,
        }

    def transcript_path(self, session_id: str = "") -> Path | None:

        ctx = self._ctx
        if ctx is None:
            return None
        sid = str(session_id or "")
        if not sid:
            try:
                sid = str(ctx.streams.current.session_id or "")
            except Exception:  # noqa: BLE001 - no session yet
                return None
        if not sid:
            return None
        return ctx.data_dir / "sessions" / sid / TRANSCRIPT_LOG

    def _persist_item(self, session_id: str, item: Any) -> None:

        ctx = self._ctx
        if ctx is None or not session_id:
            return
        path = ctx.data_dir / "sessions" / session_id / TRANSCRIPT_LOG
        record = {
            "start": None if item.start is None else round(float(item.start), 3),
            "end": None if item.end is None else round(float(item.end), 3),
            "text": item.text,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._persist_lock:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # Losing the on-disk copy must never break live transcription.
            self._logger.warning(
                "failed to persist transcript for %s", session_id, exc_info=True
            )

    def _on_session_started(self, event: Any) -> None:
        session = self._ctx.streams.get_session(str(event.get("session_id", "")))
        if session is not None:
            self._sessions[session.session_id] = session
            self._ctx.streams.set_text_enabled(
                session.session_id, StreamKind.TRANSCRIPT, self.enabled()
            )

    def _on_session_stopped(self, event: Any) -> None:
        if self._ctx is None:
            return
        session = self._ctx.streams.get_session(str(event.get("session_id", "")))
        if session is None:
            return
        self._sessions[session.session_id] = session
        self._state(session).flushing = True

    def _on_settings_changed(self, event: Any) -> None:
        key = str(event.get("key", ""))
        if key == "stt.enabled":
            threading.Thread(
                target=self._apply_enabled,
                args=(self.enabled(),),
                name="stt-enable",
                daemon=True,
            ).start()
        elif key.startswith("stt."):
            self._cancel_requests()

    def _apply_enabled(self, enabled: bool) -> None:
        session = self._processing_session or self._ctx.streams.current
        sid = session.session_id
        if not enabled:
            self._disabled_at[sid] = self._state(session).scheduled
            if self._state(session).active:
                self._state(session).done.wait(
                    float(self._setting("stt.timeout", 60.0))
                )
            self._ctx.streams.set_text_enabled(sid, StreamKind.TRANSCRIPT, False)
        else:
            start = self._disabled_at.pop(sid, session.audio.duration)
            end = session.audio.duration
            if end > start:
                self._ctx.streams.seal_text(
                    sid,
                    StreamKind.TRANSCRIPT,
                    end,
                    error={"start": start, "end": end, "error": "Source disabled"},
                )
            self._state(session).scheduled = max(self._state(session).scheduled, end)
            self._ctx.streams.set_text_enabled(sid, StreamKind.TRANSCRIPT, True)

    def _render_page(self) -> list[dict[str, Any]]:
        status = self.status()
        blocks = [
            {
                "type": BlockType.KEYVALUE,
                "items": [
                    ["状态", status["state"]],
                    ["服务", status["server_url"]],
                    ["模型", status["model"] or "未提供"],
                    ["会话", status["session_id"]],
                    ["已发送", f"{status['sent_cursor']:.3f} s"],
                    ["已接收", f"{status['received_cursor']:.3f} s"],
                    ["连续完成", f"{status['cursor']:.3f} s"],
                    ["未覆盖", f"{status['uncovered_seconds']:.3f} s"],
                    ["失败范围", str(status["failed_ranges"])],
                    ["识别标记", str(status["anomalies"])],
                    ["错误", status["error"] or "无"],
                ],
            }
        ]
        if status["partial"] is not None:
            blocks.append(
                {
                    "type": BlockType.MARKDOWN,
                    "text": "**临时文字**\n\n" + status["partial"]["text"],
                }
            )
        items = [
            i
            for i in self._ctx.streams.current.transcript.items_since(0)
            if i.source == TextSource.STT
        ][-30:]
        for item in items:
            blocks.append(
                {
                    "type": BlockType.MARKDOWN,
                    "text": f"**{_fmt_time(item.start)} - {_fmt_time(item.end)}**\n\n{item.text}",
                }
            )
        if not items and status["partial"] is None:
            blocks.append(
                {
                    "type": BlockType.TEXT,
                    "text": "暂无转写结果．开始音频输入后，定稿会显示在这里．",
                }
            )
        blocks.append(
            {
                "type": BlockType.BUTTONS,
                "buttons": [
                    {
                        "label": "打开设置",
                        "action": "ui.open_page",
                        "value": {"page_id": "settings:main"},
                        "class": "btn btn-sm",
                    }
                ],
            }
        )
        return blocks


def _fmt_time(seconds: float | None) -> str:
    value = int(seconds or 0)
    return f"{value // 60:02d}:{value % 60:02d}"


__all__ = ["STTPlugin"]
