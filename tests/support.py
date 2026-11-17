from __future__ import annotations

from typing import Any, Callable, Iterable
from core.agent_types import IpcMethod, RuntimeFrameType

from core.settings_spec import SettingsSpec


ScriptItem = dict[str, Any] | str | Callable[[dict[str, Any]], dict[str, Any]]


class FakeLLM:
    def __init__(self, script: Iterable[ScriptItem] | None = None) -> None:
        self.script: list[ScriptItem] = list(script or [])
        self.calls: list[tuple[IpcMethod, dict[str, Any]]] = []
        self.closed = False

    def _next(self, params: dict[str, Any]) -> dict[str, Any]:
        item = self.script.pop(0) if self.script else "done"
        if callable(item):
            return item(params)
        if isinstance(item, str):
            return {"content": item, "finish_reason": "stop", "model": "fake"}
        return item

    def stream(self, method: IpcMethod, *, on_sent=None, **params: Any):
        self.calls.append((method, params))
        if on_sent is not None:
            on_sent()
        if method == IpcMethod.GENERATE:
            result = self._next(params)
            yield {
                "type": RuntimeFrameType.EVENT,
                "name": "llm.delta",
                "data": {"text": result.get("content", "")},
            }
        elif method == IpcMethod.INFO:
            result = {"provider": "fake", "calls": len(self.calls)}
        else:
            result = {}
        yield {"type": RuntimeFrameType.RESULT, "result": result}

    def call(self, method: IpcMethod, **params: Any) -> dict[str, Any]:
        for message in self.stream(method, **params):
            if message["type"] == RuntimeFrameType.RESULT:
                return message["result"]
        raise RuntimeError("Fake runtime returned no result")

    def cancel(self, session_id: str) -> dict[str, Any]:
        return self.call(IpcMethod.CANCEL, session_id=session_id)

    def close(self) -> None:
        self.closed = True


class FakeSettings:
    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = dict(values or {})
        self.specs_map: dict[str, SettingsSpec] = {}
        self.changes: list[tuple[str, Any, Any]] = []

    def register(self, spec: SettingsSpec, *, plugin_id: str = "") -> SettingsSpec:
        self.specs_map[spec.key] = spec
        if spec.key not in self.values:
            self.values[spec.key] = spec.default
        return spec

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.values:
            return self.values[key]
        spec = self.specs_map.get(key)
        return spec.default if spec is not None else default

    def set(self, key: str, value: Any) -> Any:
        old = self.get(key)
        self.values[key] = value
        self.changes.append((key, old, value))
        return value

    def set_many(self, values: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for key, value in values.items():
            out[key] = self.set(key, value)
        return out

    def all(self, *, public: bool = False) -> dict[str, Any]:
        out = {}
        for key, value in self.values.items():
            spec = self.specs_map.get(key)
            if public and spec is not None and spec.secret:
                out[key] = "******" if value else ""
            else:
                out[key] = value
        return out

    def specs(self) -> list[SettingsSpec]:
        return sorted(self.specs_map.values(), key=lambda s: (s.group, s.order, s.key))
