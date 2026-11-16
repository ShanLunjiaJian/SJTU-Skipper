"""Send role-based model requests through the project OMP runtime."""

from __future__ import annotations

import dataclasses
from typing import Any

from agent_runtime.client import RuntimeClient
from .types import ChatResponse, Message, StreamEvent, Usage


class LLMService:
    def __init__(self, *, runtime: RuntimeClient):
        self.runtime = runtime

    def info(self):
        return self.runtime.call("info")

    def usage_snapshot(self):
        return {
            key: {
                "prompt_tokens": value["input"] + value["cacheRead"],
                "completion_tokens": value["output"],
                "total_tokens": value["totalTokens"],
                "cached_tokens": value["cacheRead"],
            }
            for key, value in self.runtime.call("info")["usage"].items()
        }

    def _response(self, value):
        native = value.get("usage", {})
        usage = Usage(
            prompt_tokens=int(native.get("input", 0)) + int(native.get("cacheRead", 0)),
            completion_tokens=int(native.get("output", 0)),
            total_tokens=int(native.get("totalTokens", 0)),
            cached_tokens=int(native.get("cacheRead", 0)),
        )
        key = f"{value.get('provider', '')}/{value.get('model', '')}"
        return ChatResponse(
            content=value.get("content", ""),
            model=key,
            finish_reason=value.get("finish_reason"),
            usage=usage,
        )

    def stream(
        self,
        *,
        role: str,
        messages: list[Message | dict[str, Any]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        session_id: str | None = None,
        on_sent=None,
    ):
        converted = []
        for message in messages:
            value = (
                dataclasses.asdict(message)
                if dataclasses.is_dataclass(message)
                else dict(message)
            )
            content = value.get("content")
            if isinstance(content, list):
                parts = []
                for part in content:
                    if dataclasses.is_dataclass(part):
                        part = dataclasses.asdict(part)
                    if "url" in part:
                        parts.append(
                            {"type": "image_url", "image_url": {"url": part["url"]}}
                        )
                    elif "text" in part:
                        parts.append({"type": "text", "text": part["text"]})
                    else:
                        parts.append(part)
                value["content"] = parts
            converted.append(value)
        for frame in self.runtime.stream(
            "generate",
            role=role,
            messages=converted,
            max_tokens=max_tokens,
            temperature=temperature,
            session_id=session_id,
            on_sent=on_sent,
        ):
            if frame["type"] == "result":
                yield StreamEvent(type="done", response=self._response(frame["result"]))
            elif frame.get("name") == "llm.delta":
                yield StreamEvent(type="text", text=frame["data"]["text"])
            elif frame.get("name") == "llm.reasoning":
                yield StreamEvent(type="reasoning", text=frame["data"]["text"])

    def chat(self, **kwargs):
        for event in self.stream(**kwargs):
            if event.response is not None:
                return event.response
        raise RuntimeError("Model returned no terminal response")

    def close(self):
        self.runtime.close()
