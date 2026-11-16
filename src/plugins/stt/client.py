"""HTTP uploads and synchronous WebSocket transport for the STT protocol."""

from __future__ import annotations

import json
import threading
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from .errors import SttError

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
        if event["type"] == "error":
            code = event.get("code")
            status = {
                "overloaded": 429,
                "backend": 502,
                "backend_failed": 502,
                "backend_failure": 502,
            }.get(code)
            raise SttError(str(event.get("message", code)), status_code=status)
        return event

    def finish(self) -> None:
        self._control({"type": "finish"})

    def reset(self, offset_ms: int) -> None:
        self._control({"type": "reset", "offset_ms": offset_ms})

    def _control(self, event: dict[str, Any]) -> None:
        try:
            self._socket.send(json.dumps(event))
        except (ConnectionClosed, OSError) as exc:
            raise SttError(str(exc), connection_failed=True) from exc

    def close(self) -> None:
        self._socket.close()


class STTClient:
    """Reuse one HTTP pool for concurrent uploads and track active streams."""

    def __init__(
        self,
        server_url: str,
        *,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.timeout = timeout
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._streams: set[STTStream] = set()

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
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                error = SttError(str(exc), connection_failed=True)
            except httpx.HTTPError as exc:
                raise SttError(str(exc)) from exc
            except SttError as exc:
                error = exc
            if attempt == 2 or not retryable(error):
                raise error
            if self._cancel.wait(0.25 * (2**attempt)):
                raise SttError("The STT request was cancelled.")
        raise AssertionError("Unreachable retry state.")

    def stream(
        self, *, language: str, context: str, concurrency: int, mode: str
    ) -> STTStream:
        parts = urlsplit(self.server_url)
        url = urlunsplit(
            (
                "wss" if parts.scheme == "https" else "ws",
                parts.netloc,
                parts.path + "/v1/stream",
                "mode=" + mode,
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
                    "type": "start",
                    "language": language,
                    "context": context,
                    "mode": mode,
                    "concurrency": concurrency,
                }
            )
            stream.metadata = stream.receive(self.timeout)
            if stream.metadata.get("type") != "ready":
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
