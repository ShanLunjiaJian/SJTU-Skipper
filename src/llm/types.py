"""Application message and model response data."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TextPart:
    text: str


@dataclass(frozen=True, slots=True)
class ImagePart:
    url: str
    detail: str = "auto"


@dataclass(slots=True)
class Message:
    role: str
    content: str | list[TextPart | ImagePart]


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


@dataclass(slots=True)
class ChatResponse:
    content: str
    finish_reason: str | None = None
    usage: Usage = field(default_factory=Usage)
    model: str = ""


@dataclass(slots=True)
class StreamEvent:
    type: str
    text: str = ""
    response: ChatResponse | None = None
    error: Exception | None = None
