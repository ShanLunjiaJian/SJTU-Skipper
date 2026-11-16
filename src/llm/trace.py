"""Full-fidelity, on-disk tracing of every LLM call.

One JSON file per call, written under ``<trace_dir>/<YYYY-MM-DD>/``:

* the **complete request**: every message (system / user / assistant / tool),
  every tool definition, every parameter, the resolved provider config;
* the **complete response**: content, hidden reasoning, tool calls, finish
  reason, token usage and the raw upstream body;
* timing, profile (``agent`` / ``vlm`` / …), model, and — on failure — the
  exception type, message and traceback.

Nothing is truncated: a 100k-token prompt is stored as a 100k-token prompt.
The only payload that is not inlined is inline image bytes, which are written
verbatim next to the call as binary assets and referenced by path (so a single
VLM frame does not turn one call file into megabytes of base64).

Tracing must never break a call: every write is guarded, and a failure is
reported once as a warning while the LLM call itself proceeds untouched.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import traceback as _traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .types import (
    ChatRequest,
    ChatResponse,
    ImagePart,
    Message,
    ProviderConfig,
    TextPart,
    ToolCall,
)

logger = logging.getLogger("lecture_agent.llm.trace")

_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}

_SECRET_HINTS = (
    "authorization",
    "api-key",
    "api_key",
    "apikey",
    "x-api-key",
    "token",
    "cookie",
    "secret",
    "password",
)


def _redact(headers: Mapping[str, Any]) -> dict[str, Any]:
    """Keep headers readable without writing credentials to disk."""

    out: dict[str, Any] = {}
    for name, value in headers.items():
        if any(hint in str(name).lower() for hint in _SECRET_HINTS):
            out[str(name)] = "<redacted>"
        else:
            out[str(name)] = value
    return out


def _endpoint(base_url: str) -> str:
    """Return the provider request endpoint."""
    return f"{base_url.rstrip('/')}/chat/completions" if base_url else ""


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="milliseconds")


def _tool_call_dict(call: ToolCall) -> dict[str, Any]:
    arguments = dict(call.arguments)
    try:
        arguments_json = json.dumps(arguments, ensure_ascii=False)
    except (TypeError, ValueError):
        arguments_json = repr(arguments)
    return {
        "id": call.id,
        "name": call.name,
        "arguments": arguments,
        "arguments_json": arguments_json,
    }


def _message_dict(msg: Message, assets: "_AssetSink") -> dict[str, Any]:
    out: dict[str, Any] = {"role": msg.role}
    content = msg.content
    if isinstance(content, str):
        out["content"] = content
    else:
        parts: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, TextPart):
                parts.append({"type": "text", "text": part.text})
            elif isinstance(part, ImagePart):
                parts.append(assets.image(part))
            else:  # pragma: no cover - defensive
                parts.append({"type": "text", "text": str(part)})
        out["content"] = parts
    if msg.name:
        out["name"] = msg.name
    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": dict(tc.arguments),
                },
            }
            for tc in msg.tool_calls
        ]
    if msg.tool_call_id is not None:
        out["tool_call_id"] = msg.tool_call_id
    return out


class _AssetSink:
    """Writes inline binary payloads next to the call file, by reference."""

    def __init__(self, directory: Path, stem: str) -> None:
        self._dir = directory
        self._stem = stem
        self._index = 0

    def image(self, part: ImagePart) -> dict[str, Any]:
        info: dict[str, Any] = {
            "type": "image",
            "mime_type": part.mime_type,
            "detail": part.detail,
        }
        if part.data is not None:
            index = self._index
            self._index += 1
            extension = _IMAGE_EXTENSIONS.get(part.mime_type.lower(), ".bin")
            name = f"{self._stem}-img{index}{extension}"
            self._dir.mkdir(parents=True, exist_ok=True)
            (self._dir / name).write_bytes(part.data)
            info["bytes"] = len(part.data)
            info["sha256"] = hashlib.sha256(part.data).hexdigest()
            info["file"] = f"assets/{name}"
        if part.url is not None:
            info["url"] = part.url
        return info


class TraceCall:
    """One in-flight call.  ``finish``/``fail``/``abort`` write the file once."""

    __slots__ = (
        "_tracer",
        "_written",
        "_text",
        "_reasoning",
        "_tool_calls",
        "_pending_error",
        "record",
        "path",
    )

    def __init__(self, tracer: "LLMTrace", record: dict[str, Any], path: Path) -> None:
        self._tracer = tracer
        self.record = record
        self.path = path
        self._written = False
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._tool_calls: list[ToolCall] = []
        self._pending_error: BaseException | None = None

    # -- streaming accumulation ---------------------------------------
    def on_text(self, text: str) -> None:
        if text:
            self._text.append(text)

    def on_reasoning(self, text: str) -> None:
        if text:
            self._reasoning.append(text)

    def on_tool_call(self, call: ToolCall | None) -> None:
        if call is not None:
            self._tool_calls.append(call)

    def on_error(self, error: BaseException | None) -> None:
        """Remember an ``error`` event without finalising the call.

        A stream may still be retried by the provider, and a retry that
        succeeds must not be recorded as a failure.
        """

        if error is not None:
            self._pending_error = error

    # -- terminal states ----------------------------------------------
    def finish(self, response: ChatResponse) -> None:
        try:
            self.record["response"] = _response_dict(response)
        except Exception:  # noqa: BLE001 - never let tracing break the call
            self.record["response"] = {"unrepresentable": repr(response)}
        if self._pending_error is not None:
            # The provider retried an errored attempt and then succeeded.
            self.record["retried_error"] = _error_dict(self._pending_error)
        self._close()

    def fail(self, error: BaseException) -> None:
        self.record["response"] = self._partial_response()
        self.record["error"] = _error_dict(error)
        self._close()

    def abort(self, reason: str) -> None:
        """Call never produced a terminal response (caller stopped early)."""

        self.record["response"] = self._partial_response()
        self.record["aborted"] = reason
        self._close()

    def close(self) -> None:
        """Guarantee a file even if the consumer walked away mid-stream."""

        if self._written:
            return
        if self._pending_error is not None:
            self.fail(self._pending_error)
            return
        self.abort("stream not completed")

    def _partial_response(self) -> dict[str, Any]:
        return {
            "streamed": True,
            "content": "".join(self._text),
            "reasoning": "".join(self._reasoning),
            "tool_calls": [_tool_call_dict(tc) for tc in self._tool_calls],
        }

    def _close(self) -> None:
        if self._written:
            return
        self._written = True
        now = time.time()
        self.record["finished_at"] = _iso(now)
        self.record["duration_ms"] = int((now - self.record["started_ts"]) * 1000)
        self._tracer.write(self.record, self.path)


def _response_dict(response: ChatResponse) -> dict[str, Any]:
    raw = response.raw if isinstance(response.raw, dict) else {}
    reasoning = ""
    try:
        choices = raw.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            reasoning = str(message.get("reasoning_content") or "")
    except Exception:  # noqa: BLE001 - defensive
        reasoning = ""
    return {
        "model": response.model,
        "finish_reason": response.finish_reason,
        "content": response.content,
        "reasoning": reasoning,
        "tool_calls": [_tool_call_dict(tc) for tc in response.tool_calls],
        "usage": {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
            "cached_tokens": response.usage.cached_tokens,
        },
        "raw": raw,
    }


def _error_dict(error: BaseException) -> dict[str, Any]:
    info: dict[str, Any] = {
        "type": type(error).__name__,
        "message": str(error),
        "traceback": "".join(
            _traceback.format_exception(type(error), error, error.__traceback__)
        ),
    }
    for attr in ("status_code", "body", "attempts"):
        value = getattr(error, attr, None)
        if value is not None:
            info[attr] = value if isinstance(value, (int, str)) else repr(value)
    return info


class LLMTrace:
    """Writes one complete JSON file per LLM call."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.root = Path(directory)
        self._lock = threading.Lock()
        self._seq = 0
        self._warned = False

    # ------------------------------------------------------------------
    # call lifecycle
    # ------------------------------------------------------------------
    def start(
        self,
        *,
        kind: str,
        profile: str,
        request: ChatRequest,
        config: ProviderConfig,
        provider: str,
    ) -> TraceCall:
        started = time.time()
        with self._lock:
            self._seq += 1
            seq = self._seq
        day = datetime.fromtimestamp(started).astimezone().strftime("%Y-%m-%d")
        stamp = datetime.fromtimestamp(started).astimezone().strftime("%H%M%S.%f")
        stem = f"{seq:06d}-{stamp}-{kind}"
        directory = self.root / day
        path = directory / f"{stem}.json"
        assets = _AssetSink(directory / "assets", stem)

        record: dict[str, Any] = {
            "seq": seq,
            "kind": kind,
            "profile": profile,
            "started_at": _iso(started),
            "started_ts": started,
            "pid": os.getpid(),
            "thread": threading.current_thread().name,
            "provider": provider or config.provider,
            "model": request.model or config.model,
            "endpoint": _endpoint(config.base_url),
            "session_id": request.session_id,
            "parent_session_id": request.parent_session_id,
            "request": {
                "model": request.model,
                "stream": request.stream,
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "tool_choice": request.tool_choice,
                "response_format": request.response_format,
                "extra": dict(request.extra),
                "extra_body_effective": {
                    **dict(config.extra_body),
                    **dict(request.extra),
                },
                "timeout": config.timeout,
                "max_retries": config.max_retries,
                "extra_headers": _redact(config.extra_headers),
                "messages": [_message_dict(m, assets) for m in request.messages],
                "tools": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    }
                    for t in request.tools
                ],
            },
            "file": str(path),
        }
        return TraceCall(self, record, path)

    # ------------------------------------------------------------------
    # writing
    # ------------------------------------------------------------------
    def write(self, record: dict[str, Any], path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(record, ensure_ascii=False, indent=2, default=repr)
            temporary = path.with_name(f".{path.name}.tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception as exc:  # noqa: BLE001 - logging must never break a call
            if not self._warned:
                self._warned = True
                logger.warning("failed to write LLM trace to %s: %s", path, exc)
