from __future__ import annotations

import json
import threading
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx2
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from .errors import SttError


class SttEventType(StrEnum):
    START = "start"
    FINISH = "finish"
    RESET = "reset"
    READY = "ready"
    ACCEPTED = "accepted"
    PARTIAL = "partial"
    ANOMALY = "anomaly"
    FINAL = "final"
    PROGRESS = "progress"
    DONE = "done"
    ERROR = "error"


RETRY_STATUSES = frozenset((429, 502, 503, 504))


def retryable(error: BaseException) -> bool:
    return isinstance(error, SttError) and (
        error.status_code in RETRY_STATUSES or error.connection_failed
    )


class STTStream:
    """Send PCM independently of the single event receiver."""

    def __init__(self, socket: Any) -> None:
        self._socket = socket
        self.metadata: dict[str, Any] = {}

    def send(self, pcm: bytes) -> None:
        if len(pcm) > 6400:
            raise SttError("PCM frames must not exceed 6400 bytes.")
        try:
            self._socket.send(pcm)
        except (ConnectionClosed, OSError) as exc:
            raise SttError(str(exc), connection_failed=True) from exc

    def receive(self, timeout: float | None = None) -> dict[str, Any]:
        try:
            raw = self._socket.recv(timeout=timeout)
        except TimeoutError:
            raise
        except (ConnectionClosed, OSError) as exc:
            raise SttError(str(exc), connection_failed=True) from exc
        try:
            event = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise SttError("The STT service returned an invalid event.") from exc
        if not isinstance(event, dict) or "type" not in event:
            raise SttError("The STT service returned an invalid event.")
        if event["type"] == SttEventType.ERROR:
            code = event.get("code")
            status = {
                "overloaded": 429,
                "backend": 502,
                "backend_failed": 502,
                "backend_failure": 502,
            }.get(code)
            raise SttError(str(event.get("message", code)), status_code=status)
        try:
            event["type"] = SttEventType(event["type"])
        except ValueError as exc:
            raise SttError("The STT service returned an invalid event type.") from exc
        return event

    def finish(self) -> None:
        self._control({"type": SttEventType.FINISH})

    def reset(self, offset_ms: int) -> None:
        self._control({"type": SttEventType.RESET, "offset_ms": offset_ms})

    def _control(self, event: dict[str, Any]) -> None:
        try:
            self._socket.send(json.dumps(event))
        except (ConnectionClosed, OSError) as exc:
            raise SttError(str(exc), connection_failed=True) from exc

    def close(self) -> None:
        self._socket.close()


class STTClient:
    """Compatible HTTP/WebSocket STT service adapter.

    Reuse one HTTP pool for concurrent uploads and track active streams.
    """

    name = "compatible"
    label = "兼容 STT 服务（HTTP/WebSocket）"
    supports_stream = True
    max_window = 120.0

    def __init__(
        self,
        server_url: str,
        *,
        timeout: float = 60.0,
        language: str = "Chinese",
        context: str = "",
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.timeout = timeout
        self._language = language
        self._context = context
        self._http = httpx2.Client(timeout=timeout, transport=transport)
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._streams: set[STTStream] = set()

    @staticmethod
    def configured(get: Any) -> bool:
        return bool(str(get("stt.server_url", "") or "").strip())

    @classmethod
    def describe(cls, get: Any) -> dict[str, Any]:
        return {
            "endpoint": str(get("stt.server_url", "http://127.0.0.1:8766") or ""),
            "model": "",
            "language": str(get("stt.language", "Chinese") or ""),
            "missing": "兼容 STT 服务地址为空",
        }

    @classmethod
    def create(
        cls, get: Any, *, timeout: float = 60.0, transport: Any = None
    ) -> STTClient:
        return cls(
            str(get("stt.server_url", "http://127.0.0.1:8766") or "")
            or "http://127.0.0.1:8766",
            timeout=timeout,
            language=str(get("stt.language", "Chinese") or "Chinese"),
            transport=transport,
        )

    def metadata(self, context: str = "") -> dict[str, str]:
        return {"language": self._language, "context": context}

    def transcribe(self, wav: bytes, *, language: str, context: str) -> dict[str, Any]:
        for attempt in range(3):
            if self._cancel.is_set():
                raise SttError("The STT request was cancelled.")
            try:
                response = self._http.post(
                    self.server_url + "/v1/transcriptions",
                    files=[
                        (
                            "metadata",
                            (
                                None,
                                json.dumps({"language": language, "context": context}),
                                "application/json",
                            ),
                        ),
                        ("audio", ("audio.wav", wav, "audio/wav")),
                    ],
                )
                if not response.is_success:
                    try:
                        message = response.json()["error"]["message"]
                    except (ValueError, KeyError, TypeError):
                        message = response.text
                    raise SttError(str(message), status_code=response.status_code)
                try:
                    result = response.json()
                    if not isinstance(result, dict) or not isinstance(
                        result["segments"], list
                    ):
                        raise ValueError
                    return result
                except (ValueError, KeyError, TypeError) as exc:
                    raise SttError(
                        "The STT service returned an invalid transcription."
                    ) from exc
            except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
                error = SttError(str(exc), connection_failed=True)
            except httpx2.HTTPError as exc:
                raise SttError(str(exc)) from exc
            except SttError as exc:
                error = exc
            if attempt == 2 or not retryable(error):
                raise error
            if self._cancel.wait(0.25 * (2**attempt)):
                raise SttError("The STT request was cancelled.")
        raise AssertionError("Unreachable retry state.")

    def stream(
        self, *, language: str, context: str, concurrency: int, offline: bool
    ) -> STTStream:
        parts = urlsplit(self.server_url)
        url = urlunsplit(
            (
                "wss" if parts.scheme == "https" else "ws",
                parts.netloc,
                parts.path + "/v1/stream",
                "mode=" + ("offline" if offline else "live"),
                "",
            )
        )
        try:
            socket = connect(
                url,
                open_timeout=self.timeout,
                close_timeout=1,
                max_size=1024 * 1024,
                max_queue=16,
            )
        except InvalidStatus as exc:
            raise SttError(str(exc), status_code=exc.response.status_code) from exc
        except OSError as exc:
            raise SttError(str(exc), connection_failed=True) from exc
        stream = STTStream(socket)
        with self._lock:
            if self._cancel.is_set():
                stream.close()
                raise SttError("The STT request was cancelled.")
            self._streams.add(stream)
        try:
            stream._control(
                {
                    "type": SttEventType.START,
                    "language": language,
                    "context": context,
                    "mode": "offline" if offline else "live",
                    "concurrency": concurrency,
                }
            )
            stream.metadata = stream.receive(self.timeout)
            if stream.metadata.get("type") != SttEventType.READY:
                raise SttError("The STT service did not accept the stream.")
        except BaseException:
            self.close_stream(stream)
            raise
        return stream

    def close_stream(self, stream: STTStream) -> None:
        with self._lock:
            self._streams.discard(stream)
        stream.close()

    def close(self) -> None:
        self._cancel.set()
        with self._lock:
            streams = list(self._streams)
            self._streams.clear()
        for stream in streams:
            stream.close()
        self._http.close()
