"""Application metadata for a conversation owned by the SDK."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from core.workspace import FileWorkspace


@dataclass(slots=True)
class AgentSession:
    """Keep task metadata outside the SDK context."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "default"
    role: str = "default"
    agent: str = ""
    system_prompt: str = ""
    workspace: FileWorkspace = field(default_factory=FileWorkspace)
    tools: tuple[str, ...] = ()
    model: str = ""
    created_at: float = field(default_factory=time.time)
    lock: threading.RLock = field(default_factory=threading.RLock)
    cancelled: threading.Event = field(default_factory=threading.Event)
    runtime_ready: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "model": self.model,
            "created_at": self.created_at,
        }
