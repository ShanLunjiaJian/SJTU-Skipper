from __future__ import annotations

from .exceptions import ConfigError


def _clean_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ConfigError("CANVAS_BASE_URL must start with http:// or https://.")
    return value
