"""Settings plugin package: spec contract, store, service and plugin."""

from __future__ import annotations

from .plugin import SettingsPlugin
from .spec import SECRET_MASK, SettingsSpec
from .store import SettingsService, SettingsStore

__all__ = [
    "SECRET_MASK",
    "SettingsPlugin",
    "SettingsService",
    "SettingsSpec",
    "SettingsStore",
]
