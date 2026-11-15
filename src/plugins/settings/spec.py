"""Re-export settings specification contract from :mod:`core.settings_spec`."""

from __future__ import annotations

from core.settings_spec import (
    SECRET_MASK,
    SETTING_TYPES,
    VALID_TYPES,
    SettingsSpec,
    _coerce_bool,
    _coerce_float,
    _coerce_int,
    _coerce_json,
)

__all__ = [
    "SECRET_MASK",
    "SETTING_TYPES",
    "SettingsSpec",
    "VALID_TYPES",
]
