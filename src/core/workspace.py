"""Logical file authorization shared with the SDK runtime."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterable, Mapping

from core.file_access import path_lock


class FileWorkspace:
    def __init__(
        self,
        files: Mapping[str, str | Path] | None = None,
        *,
        readonly: Iterable[str] = (),
    ) -> None:
        self._files: dict[str, Path] = {}
        self._readonly: set[str] = set(readonly)
        for name, path in (files or {}).items():
            self.register(name, path)

    def register(self, name: str, path: str | Path, *, readonly: bool = False) -> None:
        self._validate_name(name)
        self._files[name] = Path(path).absolute()
        if readonly:
            self._readonly.add(name)

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("File name must be a non-empty string")
        if os.path.isabs(name):
            raise ValueError(f"File name must not be an absolute path: {name!r}")
        if any(part in ("..", "") for part in name.replace("\\", "/").split("/")):
            raise ValueError(f"Invalid logical file name: {name!r}")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._files)

    @property
    def readonly(self) -> tuple[str, ...]:
        return tuple(name for name in self._files if name in self._readonly)

    def path(self, name: str) -> Path:
        self._validate_name(name)
        return self._files[name]

    def __contains__(self, name: str) -> bool:
        return name in self._files

    def to_dict(self) -> dict[str, Path]:
        return dict(self._files)

    def copy(self) -> FileWorkspace:
        """Copy the file map without copying file contents."""
        return FileWorkspace(self._files, readonly=self._readonly)

    def text(self, name: str) -> str:
        path = self._files.get(name)
        return path.read_text(encoding="utf-8") if path and path.exists() else ""

    def write(self, name: str, content: str) -> None:
        """Publish application-generated content with an atomic replacement."""
        if name in self._readonly:
            raise PermissionError(f"File is read-only: {name}")
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path_lock(path):
            fd, temporary = tempfile.mkstemp(
                dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
