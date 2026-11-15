"""Reader plugin: shared markdown document viewer."""

from __future__ import annotations

from .plugin import PAGE_ID, ReaderPlugin, ReaderService, split_markdown

__all__ = ["ReaderPlugin", "ReaderService", "PAGE_ID", "split_markdown"]
