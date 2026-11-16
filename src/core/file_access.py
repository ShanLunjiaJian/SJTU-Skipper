"""Provide stable locks for file mutations and note publication."""

from pathlib import Path
from threading import Lock, RLock

_registry_lock = Lock()
_locks: dict[tuple[str, Path], RLock] = {}


def _lock(namespace: str, path: Path) -> RLock:
    key = (namespace, Path(path).resolve())
    with _registry_lock:
        lock = _locks.get(key)
        if lock is None:
            lock = RLock()
            _locks[key] = lock
        return lock


def path_lock(path: Path) -> RLock:
    return _lock("path", path)


def publication_lock(notes_dir: Path) -> RLock:
    return _lock("publication", notes_dir)
