"""Selection-menu contract shared by the reader and its consumers."""

from __future__ import annotations

from enum import StrEnum


class SelectionKind(StrEnum):
    """How the browser renders one selection-menu item."""

    ACTION = "action"
    PROMPT = "prompt"
