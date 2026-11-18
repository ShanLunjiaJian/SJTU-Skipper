"""Runtime wiring: event bus, streams, UI, services and plugin DAG."""

from __future__ import annotations

import dataclasses
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from config import AppConfig
from .agent_types import IpcMethod
from .events import Clock, EventBus
from .meta import MetaRegistry
from .plugin import Plugin, PluginContext, PluginManager, resolve_plugin_spec
from .services import ServiceRegistry
from .streams import StreamHub
from .ui import UIManager

logger = logging.getLogger("lecture_agent.runtime")


class LLMProxy:
    """Resolve the shared OMP runtime after plugin registration."""

    def __init__(self, services: ServiceRegistry) -> None:
        self._services = services

    def _service(self) -> Any:
        service = self._services.get("llm")
        if service is None:
            raise RuntimeError(
                "LLM service is not registered; load the 'llm' plugin or inject a fake"
            )
        return service

    def call(self, method: IpcMethod, **params: Any) -> dict[str, Any]:
        return self._service().call(method, **params)

    def stream(self, method: IpcMethod, **params: Any):
        return self._service().stream(method, **params)

    def cancel(self, session_id: str):
        return self._service().cancel(session_id)

    def close(self) -> None:
        service = self._services.get("llm")
        if service is not None:
            closer = getattr(service, "close", None)
            if callable(closer):
                closer()


class Runtime:
    def __init__(
        self,
        *,
        config: AppConfig,
        bus: EventBus,
        clock: Clock,
        hub: StreamHub,
        ui: UIManager,
        meta: MetaRegistry,
        services: ServiceRegistry,
        llm: LLMProxy,
        plugins: PluginManager,
    ) -> None:
        self.config = config
        self.bus = bus
        self.clock = clock
        self.hub = hub
        self.ui = ui
        self.meta = meta
        self.services = services
        self.llm = llm
        self.plugins = plugins
        self._lock = threading.RLock()
        self._started = False

    @property
    def data_dir(self) -> Path:
        return self.config.data_dir

    @property
    def started(self) -> bool:
        return self._started

    @property
    def session_id(self) -> str:
        return self.hub.current.session_id

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self.bus.start()
            self.clock.start()
            try:
                order = self.plugins.load()
            except Exception:
                self.clock.stop()
                self.bus.stop()
                raise
            self._started = True
            logger.info("runtime started; plugin order: %s", " -> ".join(order))
            self.ui.refresh("runtime-started")

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            logger.info("runtime stopping")
            try:
                self.hub.end_session("runtime stopped")
            except Exception:  # noqa: BLE001
                logger.exception("failed to end session")
            self.plugins.teardown()
            self.clock.stop()
            self.llm.close()
            self.bus.stop()
            self._started = False

    def add_plugin(self, plugin: Plugin | type[Plugin] | str) -> Plugin:
        """Register a plugin; load it immediately when the runtime is running."""

        if self._started:
            return self.plugins.load_plugin(plugin)
        if isinstance(plugin, str):
            resolved, origin = resolve_plugin_spec(plugin)
            self.plugins.register(resolved, origin=origin)
            return resolved
        registered = plugin() if isinstance(plugin, type) else plugin
        self.plugins.register(registered)
        return registered

    def load_plugin(self, plugin_or_spec: Plugin | type[Plugin] | str) -> Plugin:
        """Load an external plugin into the running runtime."""

        if not self._started:
            return self.add_plugin(plugin_or_spec)
        return self.plugins.load_plugin(plugin_or_spec)

    def unload_plugin(self, plugin_id: str, *, cascade: bool = False) -> list[str]:
        """Unload a plugin (and, with *cascade*, its hard dependents)."""

        return self.plugins.unload(plugin_id, cascade=cascade)

    def reload_plugin(self, plugin_id: str) -> Plugin:
        """Hot-reload a plugin that was loaded from a module or file spec."""

        return self.plugins.reload(plugin_id)

    def status(self) -> dict[str, Any]:
        current = self.hub.current
        return {
            "runtime": {
                "started": self._started,
                "data_dir": str(self.data_dir),
                "plugin_order": self.plugins.order,
                "plugins_loaded": self.plugins.loaded,
                "services": self.services.snapshot(),
            },
            "session": current.status(),
            "sessions": [dataclasses.asdict(s) for s in self.hub.list_sessions()][-10:],
        }


def build_runtime(
    config: AppConfig,
    *,
    plugins: Iterable[Plugin | type[Plugin]] | None = None,
    llm: Any | None = None,
    audio_emit_interval: float = 0.5,
    video_emit_interval: float = 1.0,
    clock_interval: float = 1.0,
) -> Runtime:
    """Construct a runtime without starting it.

    ``llm`` can be injected for tests.  Otherwise the ``llm`` plugin must
    register the service during setup.
    """

    config.ensure_dirs()
    services = ServiceRegistry()
    if llm is not None:
        services.register("llm", llm, plugin_id="<injected>")
    llm_proxy = LLMProxy(services)
    bus = EventBus()
    hub = StreamHub(
        emit=bus.emit,
        audio_emit_interval=audio_emit_interval,
        video_emit_interval=video_emit_interval,
    )
    meta = MetaRegistry()

    def _runtime_meta() -> dict[str, Any]:
        now = datetime.now().astimezone()
        return {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "timezone": now.tzname() or "",
            "app": "sjtu-skipper",
        }

    meta.register("runtime", _runtime_meta, plugin_id="runtime")

    def on_ui_change(reason: str) -> None:
        bus.emit("ui.changed", reason=reason)

    def on_ui_notify(topic: str, payload: dict[str, Any]) -> None:
        bus.emit("ui.notify", topic=topic, payload=payload)

    ui = UIManager(on_change=on_ui_change, on_notify=on_ui_notify)

    def context_factory(plugin_id: str) -> PluginContext:
        return PluginContext(
            plugin_id=plugin_id,
            data_dir=config.data_dir,
            events=bus,
            streams=hub,
            llm=llm_proxy,
            ui=ui,
            services=services,
            meta=meta,
            logger=logging.getLogger(f"lecture_agent.plugin.{plugin_id}"),
        )

    manager = PluginManager(
        bus=bus,
        services=services,
        meta=meta,
        context_factory=context_factory,
        ui=ui,
        streams=hub,
    )

    runtime = Runtime(
        config=config,
        bus=bus,
        clock=Clock(bus, interval=clock_interval),
        hub=hub,
        ui=ui,
        meta=meta,
        services=services,
        llm=llm_proxy,
        plugins=manager,
    )

    def status_provider() -> dict[str, Any]:
        return runtime.status()

    ui.set_status_provider(status_provider, plugin_id="runtime")
    if plugins:
        manager.register_all(plugins)
    if config.plugins:
        # External plugins declared in agent.toml: ``module:Class``, ``module``
        # or ``/path/to/file.py[:Class]``.  They are loaded when the runtime
        # starts, exactly like the built-ins.
        for spec in config.plugins:
            try:
                loaded, origin = resolve_plugin_spec(spec)
                manager.register(loaded, origin=origin)
            except Exception:  # noqa: BLE001 - a bad spec must not kill startup
                logger.exception("cannot register external plugin spec %r", spec)
    return runtime
