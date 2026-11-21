import queue
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from agent_runtime.client import RuntimeClient
from core.agent_types import IpcMethod
from plugins.settings.store import SettingsStore
from core.runtime import LLMProxy


TRANSPORT_SCRIPT = """
import json
import sys

pending = {}
for line in sys.stdin:
    request = json.loads(line)
    if "callback_id" in request:
        response = {"id": pending.pop(request["callback_id"])}
        if "error" in request:
            response.update(type="error", error=request["error"])
        else:
            response.update(type="result", result=request["result"])
    elif request["method"] == "roles_sync":
        selectors = request["params"]["selectors"]
        print(json.dumps({"type": "persistence", "settings": {"roles": selectors}}), flush=True)
        response = dict(id=request["id"], type="result", result=request["params"])
    elif request["params"].get("callback"):
        callback_id = request["id"] + "-callback"
        pending[callback_id] = request["id"]
        response = dict(type="callback", id=callback_id, session_id="session",
                        role="ask_research", kind="tool", name="custom",
                        arguments={"parameters": {"value": 7}, "workspace": {}})
    else:
        response = dict(id=request["id"], type="result", result={"nested": True})
    print(json.dumps(response), flush=True)
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    runtime = RuntimeClient(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", TRANSPORT_SCRIPT],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    runtime._process = process
    runtime._reader = threading.Thread(
        target=runtime._read, args=(process,), daemon=True
    )
    monkeypatch.setattr(runtime, "_start", lambda: None)
    runtime._reader.start()
    try:
        yield runtime
    finally:
        runtime.close()
        process.stdout.close()
        process.stderr.close()


def call_callback(client):
    result = queue.Queue()

    def call():
        try:
            result.put(client.call(IpcMethod.INFO, callback=True))
        except Exception as exc:
            result.put(exc)

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    return thread, result


def test_callback_can_call_runtime(client):
    requests = []

    def callback(request):
        assert threading.current_thread() is not client._reader
        requests.append(request)
        return client.call(IpcMethod.INFO)

    client.set_callback_handler(callback)
    thread, result = call_callback(client)
    assert result.get(timeout=3) == {"nested": True}
    thread.join(3)
    assert not thread.is_alive()
    assert requests == [
        {
            "session_id": "session",
            "role": "ask_research",
            "kind": "tool",
            "name": "custom",
            "arguments": {"parameters": {"value": 7}, "workspace": {}},
        }
    ]


@pytest.mark.parametrize("handler", [None, "raises", "invalid_result"])
def test_callback_errors_do_not_stop_reader(client, handler):
    if handler == "raises":

        def callback(request):
            raise ValueError("Extension failed")
    elif handler == "invalid_result":

        def callback(request):
            return object()
    else:
        callback = None
    client.set_callback_handler(callback)
    thread, result = call_callback(client)
    error = result.get(timeout=3)
    assert isinstance(error, RuntimeError)
    if handler == "raises":
        assert str(error) == "Extension failed"
    elif handler is None:
        assert "not registered" in str(error)
    else:
        assert "not JSON serializable" in str(error)
    thread.join(3)
    assert not thread.is_alive()
    assert client.call(IpcMethod.INFO) == {"nested": True}


def test_close_releases_call_before_callback_finishes(client):
    started = threading.Event()
    release = threading.Event()
    workers = []

    def callback(request):
        workers.append(threading.current_thread())
        started.set()
        assert release.wait(3)
        return {"late": True}

    client.set_callback_handler(callback)
    thread, result = call_callback(client)
    try:
        assert started.wait(3)
        client.close()
        assert isinstance(result.get(timeout=3), RuntimeError)
        thread.join(3)
        assert not thread.is_alive()
    finally:
        release.set()
        for worker in workers:
            worker.join(3)
            assert not worker.is_alive()


def test_proxy_sets_callback_handler():
    handlers = []
    service = SimpleNamespace(set_callback_handler=handlers.append)
    proxy = LLMProxy(SimpleNamespace(get=lambda name: service))
    handler = lambda request: request
    proxy.set_callback_handler(handler)
    proxy.set_callback_handler(None)
    assert handlers == [handler, None]


def test_role_bootstrap_preserves_saved_selectors(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    saved = {
        "default": "lecture/model",
        "first": "lecture/first",
        "second": "lecture/second",
        "obsolete": 7,
    }
    store.update({"llm.roles": saved})
    runtime = RuntimeClient(tmp_path)
    runtime.register_role("one", "first", "First")
    bootstrap = runtime._bootstrap()
    assert [role["id"] for role in bootstrap["registered_roles"]] == [
        "default",
        "compaction",
        "first",
    ]
    assert bootstrap["roles"] == {**saved, "compaction": "@default"}
    normalized = {
        role: bootstrap["roles"][role] for role in ("default", "compaction", "first")
    }
    runtime._persist({"settings": {"roles": normalized}})
    runtime.register_role("two", "second", "Second", input=("text", "image"))
    assert runtime._bootstrap()["roles"]["second"] == "lecture/second"
    assert "obsolete" not in SettingsStore(tmp_path / "settings.json").get("llm.roles")


def test_role_sync_persists_without_reader_deadlock(client):
    result = queue.Queue()

    def register():
        try:
            client.register_role("plugin", "custom", "Custom")
            result.put(True)
        except Exception as exc:
            result.put(exc)

    worker = threading.Thread(target=register, daemon=True)
    worker.start()
    assert result.get(timeout=3) is True
    worker.join(3)
    assert not worker.is_alive()
    assert (
        SettingsStore(client.data_dir / "settings.json").get("llm.roles")["custom"]
        == "@default"
    )
    client.unregister_roles("plugin")
    assert "custom" not in SettingsStore(client.data_dir / "settings.json").get(
        "llm.roles"
    )


def test_role_sync_restores_later_registration(client):
    client._saved_role_selectors = {
        "default": "lecture/model",
        "first": "@second",
        "second": "lecture/second",
    }
    client.register_role("one", "first", "First")
    assert client._saved_role_selectors["first"] == "@second"
    client.register_role("two", "second", "Second")
    persisted = SettingsStore(client.data_dir / "settings.json").get("llm.roles")
    assert persisted["first"] == "@second"
    assert persisted["second"] == "lecture/second"
    client._persist({"settings": {"roles": {**persisted, "first": "lecture/updated"}}})
    client.register_role("three", "third", "Third")
    assert client._saved_role_selectors["first"] == "lecture/updated"


def test_deferred_default_and_unload_do_not_cycle(tmp_path):
    runtime = RuntimeClient(tmp_path)
    runtime._saved_role_selectors = {
        "default": "@later:high",
        "later": "lecture/model:off",
        "compaction": "@default",
    }
    assert runtime._bootstrap()["roles"]["default"] == "lecture/model:high"
    runtime._persist({"settings": {"roles": runtime._bootstrap()["roles"]}})
    runtime.register_role("plugin", "later", "Later")
    assert runtime._bootstrap()["roles"]["default"] == "@later:high"
    runtime.unregister_roles("plugin")
    assert runtime._bootstrap()["roles"]["default"] == "lecture/model:high"
    runtime.register_role("plugin", "later", "Later")
    assert runtime._bootstrap()["roles"]["later"] == "@default"


@pytest.mark.parametrize(
    "owner,role,title,formats",
    [
        ("", "custom", "Custom", ("text",)),
        ("plugin", "", "Custom", ("text",)),
        ("plugin", "custom", "", ("text",)),
        ("plugin", "custom", "Custom", ()),
        ("plugin", "custom", "Custom", ("audio",)),
        ("plugin", "custom", "Custom", ("text", "text")),
    ],
)
def test_invalid_role_registration(tmp_path, owner, role, title, formats):
    runtime = RuntimeClient(tmp_path)
    with pytest.raises(ValueError):
        runtime.register_role(owner, role, title, input=formats)
    assert [role["id"] for role in runtime.list_roles()] == ["default", "compaction"]


def test_role_ownership_and_descriptor_copies(tmp_path):
    runtime = RuntimeClient(tmp_path)
    runtime.register_role("one", "custom", "Custom")
    for owner, role in (
        ("one", "custom"),
        ("two", "custom"),
        ("one", "default"),
        ("one", "compaction"),
    ):
        with pytest.raises(ValueError, match="already registered"):
            runtime.register_role(owner, role, "Collision")
    descriptors = runtime.list_roles()
    descriptors[-1]["input"].append("image")
    assert runtime.list_roles()[-1]["input"] == ["text"]
    runtime.unregister_roles("two")
    assert runtime.list_roles()[-1]["id"] == "custom"
    runtime.unregister_roles("one")
    assert [role["id"] for role in runtime.list_roles()] == ["default", "compaction"]


def test_proxy_forwards_role_registry():
    calls = []
    roles = [{"id": "custom", "title": "Custom", "input": ["image"]}]
    service = SimpleNamespace(
        register_role=lambda *args, **kwargs: calls.append((args, kwargs)),
        unregister_roles=lambda owner: calls.append(owner),
        list_roles=lambda: roles,
    )
    proxy = LLMProxy(SimpleNamespace(get=lambda name: service))
    proxy.register_role("plugin", "custom", "Custom", input=("image",))
    assert proxy.list_roles() == roles
    proxy.unregister_roles("plugin")
    assert calls == [(("plugin", "custom", "Custom"), {"input": ("image",)}), "plugin"]


def test_unload_removes_owned_roles(tmp_path):
    from config import load_config
    from core.plugin import Plugin
    from core.runtime import build_runtime

    class RolePlugin(Plugin):
        id = "custom_plugin"

        def setup(self, ctx):
            ctx.llm.register_role(self.id, "custom", "Custom")

    backend = RuntimeClient(tmp_path)
    runtime = build_runtime(
        load_config(data_dir=tmp_path), plugins=[RolePlugin()], llm=backend
    )
    runtime.start()
    try:
        assert runtime.llm.list_roles()[-1]["id"] == "custom"
        runtime.unload_plugin("custom_plugin")
        assert [role["id"] for role in runtime.llm.list_roles()] == [
            "default",
            "compaction",
        ]
    finally:
        runtime.stop()


def test_user_update_replaces_deferred_selector(client):
    client._saved_role_selectors = {
        "default": "lecture/model",
        "first": "@second",
        "second": "lecture/second",
    }
    client.register_role("one", "first", "First")
    client._persist(
        {
            "settings": {
                "roles": {
                    "default": "lecture/model",
                    "compaction": "@default",
                    "first": "lecture/updated",
                }
            }
        }
    )
    client.register_role("two", "second", "Second")
    assert (
        SettingsStore(client.data_dir / "settings.json").get("llm.roles")["first"]
        == "lecture/updated"
    )


def test_unload_clears_transitive_default_aliases(tmp_path):
    runtime = RuntimeClient(tmp_path)
    runtime._saved_role_selectors = {
        "default": "@middle",
        "middle": "@last",
        "last": "lecture/model",
    }
    runtime.register_role("middle_plugin", "middle", "Middle")
    runtime.register_role("last_plugin", "last", "Last")
    runtime.unregister_roles("last_plugin")
    assert runtime._bootstrap()["roles"] == {
        "default": "lecture/model",
        "compaction": "@default",
        "middle": "@default",
    }
