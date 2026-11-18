"""STT provider registry.

The STT core owns one pipeline (worker, coverage, audio gate, transcript and
UI).  Providers only translate an external protocol into the normalized batch
or stream interface:

* every provider implements ``configured``, ``metadata``, ``transcribe``,
  ``close``;
* streaming providers additionally implement ``stream`` and set
  ``supports_stream = True``;
* ``max_window`` is the longest audio span a single batch request accepts.

Adding a provider means adding one class here; the core does not change.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .client import STTClient
from .errors import SttError
from .google_cloud import GoogleProvider

Settings = Callable[[str, Any], Any]

PROVIDERS: tuple[type, ...] = (STTClient, GoogleProvider)

PROVIDER_OPTIONS: tuple[tuple[str, str], ...] = tuple(
    (provider.name, provider.label) for provider in PROVIDERS
)


def provider_names() -> tuple[str, ...]:
    return tuple(provider.name for provider in PROVIDERS)


def provider_class(name: str) -> type:
    for provider in PROVIDERS:
        if provider.name == name:
            return provider
    raise SttError(f"Unknown STT provider {name!r}.")


def provider_label(name: str) -> str:
    try:
        return str(provider_class(name).label)
    except SttError:
        return name


def supports_stream(name: str) -> bool:
    try:
        return bool(provider_class(name).supports_stream)
    except SttError:
        return False


def configured(name: str, get: Settings) -> bool:
    try:
        return bool(provider_class(name).configured(get))
    except SttError:
        return False


def describe(name: str, get: Settings) -> dict[str, Any]:
    """Cheap provider description for status/UI; never opens a connection."""

    try:
        return dict(provider_class(name).describe(get))
    except SttError:
        return {}


def create_provider(
    name: str,
    *,
    get: Settings,
    timeout: float = 60.0,
    transport: Any = None,
) -> Any:
    provider = provider_class(name)
    return provider.create(get, timeout=timeout, transport=transport)


__all__ = [
    "PROVIDERS",
    "PROVIDER_OPTIONS",
    "configured",
    "create_provider",
    "describe",
    "provider_class",
    "provider_label",
    "provider_names",
    "supports_stream",
]
