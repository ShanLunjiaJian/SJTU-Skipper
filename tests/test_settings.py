"""Tests for the P2 settings plugin (store, service, UI)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from config import load_config
from core.runtime import build_runtime
from core.settings_spec import SECRET_MASK, SettingsSpec
from plugins.settings import (
    SettingsPlugin,
    SettingsService,
    SettingsStore,
)

GROUPED = [
    SettingsSpec(
        key="llm.model",
        label="模型",
        type="string",
        default="deepseek-flash",
        group="LLM",
        order=1,
    ),
    SettingsSpec(
        key="llm.api_key",
        label="API Key",
        type="secret",
        default="",
        group="LLM",
        order=2,
        placeholder="key...",
    ),
    SettingsSpec(
        key="input.keep_recordings",
        label="本地保留录像",
        type="float",
        default=1.0,
        group="输入",
        order=1,
    ),
]


def make_service(tmp_path: Path, specs=()) -> SettingsService:
    store = SettingsStore(tmp_path / "settings.json")
    service = SettingsService(store)
    for spec in specs:
        service.register(spec)
    return service


def make_runtime(tmp_path: Path):
    cfg = load_config(data_dir=tmp_path)
    runtime = build_runtime(cfg, plugins=[SettingsPlugin()])
    runtime.start()
    return runtime


# ----------------------------------------------------------------------
# Spec / service
# ----------------------------------------------------------------------
def test_register_returns_spec_and_get_falls_back_to_default(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    spec = SettingsSpec(key="llm.model", label="模型", default="deepseek-flash")
    assert service.register(spec) is spec
    assert service.get("llm.model") == "deepseek-flash"
    assert service.get("unknown.key") is None
    assert service.get("unknown.key", "fallback") == "fallback"
    assert [s.key for s in service.specs()] == ["llm.model"]
    assert service.get_spec("llm.model") is spec
    # Defaults must not be persisted before an explicit set().
    assert not (tmp_path / "settings.json").exists()


def test_set_persists_atomically_and_reloads(tmp_path: Path) -> None:
    service = make_service(
        tmp_path, [SettingsSpec(key="a.b", label="B", type="int", default=1)]
    )
    service.set("a.b", 7)
    path = tmp_path / "settings.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"a.b": 7}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # No temp files left behind (atomic tmp + os.replace).
    assert list(tmp_path.glob(".settings.json.*")) == []

    reloaded = SettingsService(SettingsStore(path))
    assert reloaded._store.get("a.b") == 7
    assert reloaded.get("a.b") == 7
    assert reloaded.get("missing") is None


def test_set_many_is_atomic_on_validation_error(tmp_path: Path) -> None:
    service = make_service(
        tmp_path,
        [
            SettingsSpec(key="x.n", label="N", type="int", default=0),
            SettingsSpec(key="x.m", label="M", type="int", default=0),
        ],
    )
    with pytest.raises(ValueError):
        service.set_many({"x.n": "3", "x.m": "not-a-number"})
    assert service.get("x.n") == 0
    assert not (tmp_path / "settings.json").exists()

    assert service.set_many({"x.n": "3", "x.m": "4"}) == {"x.n": 3, "x.m": 4}
    assert service.all() == {"x.n": 3, "x.m": 4}


@pytest.mark.parametrize(
    ("spec_type", "default", "raw", "expected"),
    [
        ("string", "", 12, "12"),
        ("text", "", None, ""),
        ("int", 0, "42", 42),
        ("int", 0, 7.0, 7),
        ("int", 0, 7.9, ValueError),
        ("int", 0, "abc", ValueError),
        ("float", 0.0, "1.5", 1.5),
        ("float", 0.0, 2, 2.0),
        ("float", 0.0, "nan", ValueError),
        ("bool", False, "true", True),
        ("bool", False, 0, False),
        ("json", None, '{"a": [1, 2]}', {"a": [1, 2]}),
        ("json", None, "", None),
        ("json", None, "{bad", ValueError),
    ],
)
def test_type_conversion(tmp_path: Path, spec_type, default, raw, expected) -> None:
    service = make_service(
        tmp_path,
        [SettingsSpec(key="k", label="K", type=spec_type, default=default)],
    )
    if expected is ValueError:
        with pytest.raises(ValueError):
            service.set("k", raw)
        assert service.get("k") == default
    else:
        assert service.set("k", raw) == expected
        assert service.get("k") == expected
        assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8")) == {
            "k": expected
        }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("yes", True),
        ("ON", True),
        ("1", True),
        ("off", False),
        ("FALSE", False),
        ("0", False),
        ("", False),
        (2, True),
        (True, True),
    ],
)
def test_bool_strings(tmp_path: Path, raw, expected) -> None:
    service = make_service(
        tmp_path, [SettingsSpec(key="b", label="B", type="bool", default=False)]
    )
    assert service.set("b", raw) is expected


def test_select_options_and_invalid_value(tmp_path: Path) -> None:
    service = make_service(
        tmp_path,
        [
            SettingsSpec(
                key="s",
                label="S",
                type="select",
                default="a",
                options=("a", "b"),
            ),
            SettingsSpec(
                key="n",
                label="N",
                type="select",
                default=1,
                options=(1, 2),
            ),
        ],
    )
    assert service.set("s", "b") == "b"
    assert service.set("n", "2") == 2
    with pytest.raises(ValueError):
        service.set("s", "z")
    with pytest.raises(ValueError):
        service.set_many({"s": "c"})
    assert service.get("s") == "b"


def test_secret_masking_and_internal_value(tmp_path: Path) -> None:
    service = make_service(
        tmp_path,
        [
            SettingsSpec(key="llm.api_key", label="API Key", type="secret", default=""),
            SettingsSpec(key="llm.model", label="模型", default="m"),
        ],
    )
    assert service.all(public=True) == {"llm.model": "m", "llm.api_key": ""}
    service.set("llm.api_key", "real-token")
    assert service.get("llm.api_key") == "real-token"
    assert service.all()["llm.api_key"] == "real-token"
    assert service.all(public=True)["llm.api_key"] == SECRET_MASK
    described = {item["key"]: item for item in service.describe()}
    assert described["llm.api_key"]["value"] == SECRET_MASK
    assert described["llm.api_key"]["is_set"] is True
    assert described["llm.model"]["value"] == "m"


def test_set_many_keeps_secret_when_empty_or_masked(tmp_path: Path) -> None:
    service = make_service(
        tmp_path,
        [SettingsSpec(key="llm.api_key", label="API Key", type="secret", default="")],
    )
    service.set("llm.api_key", "secret-value")
    assert service.set_many({"llm.api_key": ""}) == {}
    assert service.set_many({"llm.api_key": SECRET_MASK}) == {}
    assert service.get("llm.api_key") == "secret-value"
    # An explicit (non-empty) value replaces it, and "" via set() clears it.
    service.set_many({"llm.api_key": "new-secret"})
    assert service.get("llm.api_key") == "new-secret"
    service.set("llm.api_key", "")
    assert service.get("llm.api_key") == ""


def test_corrupt_json_falls_back_and_backs_up(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not json at all", encoding="utf-8")

    store = SettingsStore(path)
    assert store.to_dict() == {}
    backups = list(tmp_path.glob("settings.json.corrupt-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not json at all"

    service = SettingsService(store)
    service.register(SettingsSpec(key="k", label="K", default="v"))
    assert service.get("k") == "v"
    service.set("k", "written")
    assert json.loads(path.read_text(encoding="utf-8")) == {"k": "written"}


# ----------------------------------------------------------------------
# Plugin wiring / UI
# ----------------------------------------------------------------------
def test_ui_state_has_form_per_group_and_toolbox_items(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    try:
        service = runtime.services.require("settings")
        for spec in GROUPED:
            service.register(spec)
        service.set("llm.api_key", "secret-value")

        state = runtime.ui.state()
        # No settings tab / toolbox item: only the page (opened via palette).
        assert all(t["id"] != "settings" for t in state["right_tabs"])
        assert all(
            i["id"] != "settings.open"
            for t in state["right_tabs"]
            for i in t["toolbox"]
        )

        runtime.ui.open_page("settings:main")
        blocks = runtime.ui.state()["page_content"]["settings:main"]
        assert blocks[0]["type"] == "tabs"
        assert blocks[0]["id"] == "settings.groups"
        forms = [
            block
            for group_tab in blocks[0]["tabs"]
            for block in group_tab["blocks"]
            if block["type"] == "form"
        ]
        assert len(forms) == len({spec.group for spec in GROUPED})
        fields = {field["name"]: field for form in forms for field in form["fields"]}
        assert set(fields) == {spec.key for spec in GROUPED}
        field = fields["llm.api_key"]
        assert field["secret"] is True
        assert field["label"] == "API Key"
        assert field["type"] == "secret"
        assert field["value"] == ""
        assert field["secret_length"] == len("secret-value")
        assert field["is_set"] is True
        assert field["placeholder"] == "key..."
        assert fields["llm.model"]["value"] == "deepseek-flash"
        assert forms[0]["submit"]["action"] == "settings.save"
    finally:
        runtime.stop()


def test_action_save_converts_and_keeps_secret(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    try:
        service = runtime.services.require("settings")
        for spec in GROUPED:
            service.register(spec)
        service.set("llm.api_key", "secret-value")

        result = runtime.ui.dispatch_action(
            "settings.save",
            {
                "values": {
                    "llm.model": "deepseek-reasoner",
                    "llm.api_key": SECRET_MASK,
                    "input.keep_recordings": "3",
                }
            },
        )
        assert result.ok is True
        assert service.get("llm.model") == "deepseek-reasoner"
        assert service.get("llm.api_key") == "secret-value"
        assert service.get("input.keep_recordings") == 3

        # An empty secret means "keep existing", not "clear".
        kept = runtime.ui.dispatch_action(
            "settings.save", {"values": {"llm.api_key": ""}}
        )
        assert kept.ok is True
        assert service.get("llm.api_key") == "secret-value"

        cleared = runtime.ui.dispatch_action(
            "settings.clear_secret", {"values": {"key": "llm.api_key"}}
        )
        assert cleared.ok is True
        assert service.get("llm.api_key") == ""

        bad = runtime.ui.dispatch_action(
            "settings.save",
            {"values": {"input.keep_recordings": "fast"}},
        )
        assert bad.ok is False
        assert "本地保留录像" in bad.message
        assert service.get("input.keep_recordings") == 3

        empty = runtime.ui.dispatch_action("settings.save", {"values": {}})
        assert empty.ok is True
    finally:
        runtime.stop()


def test_settings_changed_event_payload(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    try:
        service = runtime.services.require("settings")
        service.register(SettingsSpec(key="x.n", label="N", type="int", default=1))
        events: list[dict] = []
        runtime.bus.on(
            "settings.changed", lambda event: events.append(dict(event.payload))
        )

        service.set("x.n", "5")
        assert runtime.bus.flush(2.0)
        assert events == [{"key": "x.n", "old": 1, "new": 5}]

        # Unchanged values do not emit; resetting back to the default does.
        service.set("x.n", 5)
        assert runtime.bus.flush(2.0)
        assert len(events) == 1
        service.reset("x.n")
        assert runtime.bus.flush(2.0)
        assert events[-1] == {"key": "x.n", "old": 5, "new": 1}
    finally:
        runtime.stop()


def test_ui_refresh_after_settings_change(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    try:
        service = runtime.services.require("settings")
        before = runtime.ui.state()["version"]
        service.register(SettingsSpec(key="x.n", label="N", type="int", default=0))
        after_register = runtime.ui.state()["version"]
        assert after_register > before
        service.set("x.n", 3)
        assert runtime.ui.state()["version"] > after_register
    finally:
        runtime.stop()


def test_setting_persists_across_runtime_restart(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    try:
        service = runtime.services.require("settings")
        service.register(
            SettingsSpec(key="llm.model", label="模型", default="deepseek-flash")
        )
        service.set("llm.model", "qwen")
    finally:
        runtime.stop()

    runtime = make_runtime(tmp_path)
    try:
        service = runtime.services.require("settings")
        service.register(
            SettingsSpec(key="llm.model", label="模型", default="deepseek-flash")
        )
        assert service.get("llm.model") == "qwen"
        assert stat.S_IMODE((tmp_path / "settings.json").stat().st_mode) == 0o600
    finally:
        runtime.stop()


def test_action_and_readonly_settings_fields(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    try:
        service = runtime.services.require("settings")
        service.register(
            SettingsSpec(
                key="demo.status",
                label="状态",
                type="string",
                default="原始",
                readonly=True,
                group="Demo",
            )
        )
        service.register(
            SettingsSpec(
                key="demo.login",
                label="登录",
                type="action",
                action="demo.login",
                button_label="去登录",
                group="Demo",
            )
        )
        runtime.ui.open_page("settings:main")
        blocks = runtime.ui.state()["page_content"]["settings:main"]
        forms = [
            block
            for tab in blocks[0]["tabs"]
            for block in tab["blocks"]
            if block["type"] == "form"
        ]
        fields = {field["key"]: field for form in forms for field in form["fields"]}
        assert fields["demo.status"]["readonly"] is True
        assert fields["demo.login"]["type"] == "action"
        assert fields["demo.login"]["action"] == "demo.login"
        assert fields["demo.login"]["button_label"] == "去登录"
        assert "name" not in fields["demo.login"]

        result = runtime.ui.dispatch_action(
            "settings.save", {"values": {"demo.status": "被篡改"}}
        )
        assert result.ok is True
        assert service.get("demo.status") == "原始"
    finally:
        runtime.stop()


class ConfigurationRuntime:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.calls: list[tuple[str, dict]] = []
        self.fail_configure = False
        self.closed = False
        self.configuration = {
            "models_yaml": "providers: {}\n",
            "roles_yaml": "default: lecture/example\n",
            "providers": [
                {
                    "id": "lecture",
                    "auth": "apiKey",
                    "configured": True,
                    "source": "auth",
                },
                {
                    "id": "opencode",
                    "auth": "oauth",
                    "configured": False,
                    "source": "none",
                },
            ],
            "roles": {
                "agent_chat": {
                    "provider": "lecture",
                    "model": "example",
                    "thinkingLevel": "high",
                }
            },
        }

    def call(self, method: str, **params):
        self.calls.append((method, params))
        if method == "configure":
            if self.fail_configure:
                raise ValueError("Invalid role selector")
            self.configuration.update(
                models_yaml=params["models"], roles_yaml=params["roles"]
            )
        return dict(self.configuration)

    def close(self):
        self.closed = True


def make_llm_settings_runtime(tmp_path, monkeypatch):
    from plugins.llm_core import plugin as llm_plugin

    class Service:
        def __init__(self, *, runtime):
            self.runtime = runtime

        def close(self):
            self.runtime.close()

        def info(self):
            return self.runtime.call("info")

    monkeypatch.setattr(llm_plugin, "RuntimeClient", ConfigurationRuntime)
    monkeypatch.setattr(llm_plugin, "LLMService", Service)
    plugin = llm_plugin.LLMPlugin()
    runtime = build_runtime(
        load_config(data_dir=tmp_path), plugins=[SettingsPlugin(), plugin]
    )
    runtime.start()
    return runtime, plugin, runtime.services.require("llm").runtime


def test_external_yaml_editors_save_without_settings_copies(tmp_path, monkeypatch):
    runtime, _plugin, backend = make_llm_settings_runtime(tmp_path, monkeypatch)
    try:
        runtime.ui.open_page("settings:main")
        blocks = runtime.ui.state()["page_content"]["settings:main"][0]["tabs"][0][
            "blocks"
        ]
        editors = next(
            block
            for block in blocks
            if block.get("submit", {}).get("action") == "llm.configure"
        )
        assert {field["name"] for field in editors["fields"]} == {
            "models_yaml",
            "roles_yaml",
        }
        assert all(field["input"] == "textarea" for field in editors["fields"])
        assert "lecture/example:high" in json.dumps(blocks)

        result = runtime.ui.dispatch_action(
            "llm.configure",
            {
                "values": {
                    "models_yaml": "providers: {lecture: {}}\n",
                    "roles_yaml": "default: lecture/example\n",
                }
            },
        )
        assert result.ok
        assert backend.calls[-1] == (
            "configure",
            {
                "models": "providers: {lecture: {}}\n",
                "roles": "default: lecture/example\n",
            },
        )
        service = runtime.services.require("settings")
        assert "models_yaml" not in service.all()
        assert "roles_yaml" not in service.all()
        rejected = runtime.ui.dispatch_action(
            "settings.save", {"values": {"models_yaml": "duplicate"}}
        )
        assert not rejected.ok
        assert "models_yaml" not in service.all()
    finally:
        runtime.stop()


def test_invalid_yaml_keeps_effective_configuration(tmp_path, monkeypatch):
    runtime, _plugin, backend = make_llm_settings_runtime(tmp_path, monkeypatch)
    try:
        previous = dict(backend.configuration)
        backend.fail_configure = True
        result = runtime.ui.dispatch_action(
            "llm.configure",
            {
                "values": {
                    "models_yaml": "broken",
                    "roles_yaml": "invalid",
                }
            },
        )
        assert not result.ok
        assert "Invalid role selector" in result.message
        assert backend.configuration == previous
        assert runtime.services.require("settings").all() == {}
    finally:
        runtime.stop()


def test_provider_credentials_are_write_only_runtime_actions(tmp_path, monkeypatch):
    runtime, _plugin, backend = make_llm_settings_runtime(tmp_path, monkeypatch)
    try:
        result = runtime.ui.dispatch_action(
            "llm.credentials", {"values": {"provider": "lecture", "key": "secret-key"}}
        )
        assert result.ok
        assert backend.calls[-1] == (
            "credentials",
            {"provider": "lecture", "key": "secret-key"},
        )
        runtime.ui.open_page("settings:main")
        assert "secret-key" not in json.dumps(runtime.ui.state(), ensure_ascii=False)
        assert runtime.services.require("settings").all() == {}
        result = runtime.ui.dispatch_action(
            "llm.logout", {"values": {"provider": "lecture"}}
        )
        assert result.ok
        assert backend.calls[-1] == ("logout", {"provider": "lecture"})
    finally:
        runtime.stop()


def test_device_login_imports_oauth_into_runtime(tmp_path, monkeypatch):
    from integrations.opencode_go import DeviceFlow, TokenSet
    from plugins.llm_core import plugin as module

    flow = DeviceFlow("device", "code", "https://opencode.ai/verify", 1, 60)
    monkeypatch.setattr(module, "start_device_flow", lambda: flow)
    monkeypatch.setattr(
        module,
        "poll_device_token",
        lambda _: TokenSet("access", "refresh", 9999, "org"),
    )
    monkeypatch.setattr(module.webbrowser, "open", lambda _: False)
    runtime, plugin, backend = make_llm_settings_runtime(tmp_path, monkeypatch)
    try:
        result = runtime.ui.dispatch_action(
            "llm.opencode_login", {"values": {"provider": "opencode"}}
        )
        assert result.ok
        plugin._login_thread.join(timeout=2)
        assert not plugin._login_thread.is_alive()
        assert (
            "import_credentials",
            {
                "provider": "opencode",
                "credential": {
                    "type": "oauth",
                    "access": "access",
                    "refresh": "refresh",
                    "expires": 9999,
                    "org_id": "org",
                },
            },
        ) in backend.calls
        assert runtime.services.require("settings").all() == {}
        result = runtime.ui.dispatch_action(
            "llm.logout", {"values": {"provider": "opencode"}}
        )
        assert result.ok
        assert backend.calls[-1] == ("logout", {"provider": "opencode"})
    finally:
        runtime.stop()
