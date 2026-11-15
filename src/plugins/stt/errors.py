"""Shared STT errors."""

from __future__ import annotations


class SttError(RuntimeError):
    """The STT service or transport rejected a request."""

    def __init__(
        self, message: str, *, status_code: int | None = None,
        connection_failed: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.connection_failed = connection_failed


__all__ = ["SttError"]
