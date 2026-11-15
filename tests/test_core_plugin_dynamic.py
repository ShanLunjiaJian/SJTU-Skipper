"""Framework-level tests: scoped contexts, teardown cleanup and hot plugging."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from config import load_config
from core.events import EventBus
from core.plugin import Plugin, PluginContext, PluginError
from core.runtime import build_runtime
from core.services import ServiceError, ServiceRegistry
from core.streams import StreamHub
from core.ui import UIManager


# ---------------------------------------------------------------------------
# Scoped context: plugin_id is injected, contributions are removable
# ---------------------------------------------------------------------------


class ScopedPlugin(Plugin):
    id = "scoped"
    provides = ("scoped",)

    def setup(self, ctx: PluginContext) -> None:
        # Every call below omits plugin_id on purpose.
        ctx.register_service("scoped", object())
        ctx.ui.add_tab("left", "scoped.tab", "范围")
        ctx.ui.add_page("scoped.page", "范围页", render=lambda: "hi")
        ctx.ui.register_action("scoped.ping", lambda payload: {"ok": True})
        ctx.ui.register_command("scoped.cmd", "命令", page_id="scoped.page")
        ctx.ui.register_task_provider(lambda: [{"id": "t"}])
        ctx.events.on("scoped.event", lambda event: None)
        ctx.meta.register("scoped", lambda: "ok")


def test_context_injects_plugin_id_and_cleanup_removes_everything(tmp_path: Path) -> None:
    runtime = build_runtime(load_config(data_dir=tmp_path), plugins=[ScopedPlugin()])
    runtime.start()
    try:
        snapshot = runtime.ui.snapshot()
        assert snapshot["tabs"] == {"scoped.tab": "left"}
        assert "scoped.page" in snapshot["pages"]
        assert "scoped.ping" in snapshot["actions"]
        assert runtime.bus.handler_count("scoped.event") == 1
        assert runtime.services.owner("scoped") == "scoped"
        assert runtime.ui.state()["tasks"] == [{"id": "t"}]
    finally:
        runtime.stop()

    assert "scoped.tab" not in runtime.ui.snapshot()["tabs"]
    assert "scoped.ping" not in runtime.ui.snapshot()["actions"]
    assert runtime.ui.snapshot()["commands"] == []
    assert runtime.ui.state()["tasks"] == []
    assert runtime.bus.handler_count("scoped.event") == 0
    assert runtime.services.get("scoped") is None
    # The built-in navigation action survives (it belongs to "core").
    assert runtime.ui.has_action("ui.open_page")


def test_foreign_service_requires_declaration(tmp_path: Path) -> None:
    class Snoop(Plugin):
        id = "snoop"

        def setup(self, ctx: PluginContext) -> None:
            ctx.require("scoped")

    runtime = build_runtime(
        load_config(data_dir=tmp_path), plugins=[ScopedPlugin(), Snoop()]
    )
    with pytest.raises(PluginError, match="cannot access service"):
        runtime.start()
    assert runtime.started is False


def test_declared_soft_dependency_allows_access(tmp_path: Path) -> None:
    seen: list[object] = []

    class Snoop(Plugin):
        id = "snoop"
        optional_requires = ("scoped",)

        def setup(self, ctx: PluginContext) -> None:
            seen.append(ctx.require("scoped"))

    runtime = build_runtime(
        load_config(data_dir=tmp_path), plugins=[ScopedPlugin(), Snoop()]
    )
    runtime.start()
    try:
        assert runtime.plugins.order.index("scoped") < runtime.plugins.order.index("snoop")
        assert seen
    finally:
        runtime.stop()


def test_register_service_must_match_declared_provides() -> None:
    class WrongName(Plugin):
        id = "wrong"
        provides = ("wrong",)

    bus = EventBus()
    ctx = PluginContext(
        plugin_id="wrong",
        data_dir=Path("."),
        events=bus,
        streams=StreamHub(),
        llm=None,
        ui=UIManager(),
        services=ServiceRegistry(),
        meta=__import__("core.meta", fromlist=["MetaRegistry"]).MetaRegistry(),
        logger=__import__("logging").getLogger("t"),
        declared_provides=("wrong",),
    )
    with pytest.raises(PluginError, match="declared provides"):
        ctx.register_service("other", object())
    with pytest.raises(PluginError, match="narrow service facade"):
        ctx.register_service("wrong", WrongName())


# ---------------------------------------------------------------------------
# Shared tabs
# ---------------------------------------------------------------------------


def test_shared_tab_keeps_other_owner_and_drops_when_last_leaves() -> None:
    ui = UIManager()
    ui.add_tab("right", "note", "笔记", plugin_id="summarize")
    ui.add_toolbox_item("note", "note.a", "A", plugin_id="summarize")
    ui.add_tab("right", "note", "笔记", plugin_id="instant-summarize")
    ui.add_toolbox_item("note", "note.b", "B", plugin_id="instant-summarize")
    assert len(ui.state()["right_tabs"][0]["toolbox"]) == 2

    ui.remove_plugin("summarize")
    assert ui.has_tab("note")
    assert not ui.has_toolbox_item("note.a")

    ui.remove_plugin("instant-summarize")
    assert not ui.has_tab("note")


# ---------------------------------------------------------------------------
# Services and events
# ---------------------------------------------------------------------------


def test_service_registry_replace_injected_and_refuse_private_calls() -> None:
    registry = ServiceRegistry()
    registry.register("llm", object(), plugin_id="<injected>")
    real = object()
    registry.register("llm", real, plugin_id="llm")
    assert registry.get("llm") is real
    with pytest.raises(ServiceError):
        registry.register("llm", object(), plugin_id="other")

    class Facade:
        def ping(self) -> str:
            return "pong"

        def _secret(self) -> str:  # pragma: no cover - must stay unreachable
            return "nope"

    registry.register("svc", Facade(), plugin_id="plug")
    assert registry.call("svc", "ping") == "pong"
    with pytest.raises(ServiceError, match="private method"):
        registry.call("svc", "_secret")
    assert registry.remove_plugin("plug") == ["svc"]
    assert registry.get("svc") is None


def test_event_bus_drops_subscriptions_physically() -> None:
    bus = EventBus()
    subs = [bus.on("x", lambda event: None, plugin_id="p") for _ in range(3)]
    bus.on("*", lambda event: None, plugin_id="p")
    bus.on("x", lambda event: None, plugin_id="q")
    assert bus.handler_count("x") == 5

    bus.remove_plugin("p")
    assert bus.handler_count("x") == 1
    assert all(not sub.active for sub in subs)

    for sub in list(bus._handlers.get("x", [])):
        sub.cancel()
    assert bus.handler_count() == 0
    assert not bus._handlers


def test_stream_hub_media_consumers() -> None:
    hub = StreamHub()
    hub.register_consumer(
        "stt", "audio", cursor_fn=lambda: 4.0, busy_fn=lambda: True,
        gate_limit_fn=lambda: 30.0,
    )
    hub.register_consumer(
        "vlm", "audio", cursor_fn=lambda: 2.0, busy_fn=lambda: False,
        gate_limit_fn=lambda: 60.0,
    )
    assert hub.consumer_cursor("audio") == 2.0
    assert hub.consumer_gate_limit("audio") == 60.0
    assert hub.consumers_busy() == ["stt"]
    assert hub.consumer_cursor("video") == 0.0

    hub.remove_plugin("stt")
    assert hub.consumers_busy() == []
    assert hub.consumer_cursor("audio") == 2.0


# ---------------------------------------------------------------------------
# Dynamic load / unload / reload
# ---------------------------------------------------------------------------

_EXTERNAL = """
from core.plugin import Plugin, PluginContext


class External(Plugin):
    id = "external"
    provides = ("external",)

    def setup(self, ctx: PluginContext) -> None:
        ctx.register_service("external", object())
        ctx.ui.add_tab("right", "external", "外部")

    def teardown(self) -> None:
        pass
"""


def _write_external(path: Path, body: str = _EXTERNAL) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_hot_plug_unload_and_reload(tmp_path: Path) -> None:
    plugin_file = _write_external(tmp_path / "ext_plugin.py")
    runtime = build_runtime(load_config(data_dir=tmp_path))
    runtime.start()
    try:
        loaded = runtime.load_plugin(str(plugin_file))
        assert loaded.id == "external"
        assert "external" in runtime.plugins.loaded
        assert runtime.ui.has_tab("external")

        class Dependent(Plugin):
            id = "dependent"
            requires = ("external",)

            def setup(self, ctx: PluginContext) -> None:
                pass

        runtime.add_plugin(Dependent())
        assert runtime.plugins.loaded[-1] == "dependent"

        with pytest.raises(PluginError, match="required by"):
            runtime.unload_plugin("external")

        removed = runtime.unload_plugin("external", cascade=True)
        assert removed == ["dependent", "external"]
        assert "external" not in runtime.services
        assert not runtime.ui.has_tab("external")

        runtime.load_plugin(str(plugin_file))
        assert runtime.reload_plugin("external").id == "external"
        assert runtime.ui.has_tab("external")
    finally:
        runtime.stop()


def test_dynamic_load_validates_dependencies(tmp_path: Path) -> None:
    runtime = build_runtime(load_config(data_dir=tmp_path))
    runtime.start()

    class Orphan(Plugin):
        id = "orphan"
        requires = ("missing",)

    try:
        with pytest.raises(PluginError, match="not loaded"):
            runtime.load_plugin(Orphan())
        assert "orphan" not in runtime.plugins.loaded
    finally:
        runtime.stop()


def test_missing_optional_dependency_is_not_an_error(tmp_path: Path) -> None:
    class Soft(Plugin):
        id = "soft"
        optional_requires = ("absent",)

        def setup(self, ctx: PluginContext) -> None:
            pass

    runtime = build_runtime(load_config(data_dir=tmp_path), plugins=[Soft()])
    runtime.start()
    try:
        assert runtime.plugins.loaded == ["soft"]
    finally:
        runtime.stop()