"""Application contracts for SDK sessions and file publication."""

from __future__ import annotations

import threading

import pytest

from config import load_config
from core.agent_types import AgentProfile, AgentTool
from core.plugin import Plugin
from core.runtime import build_runtime
from core.workspace import FileWorkspace
from plugins.agent import AgentPlugin
from tests.support import FakeLLM, FakeSettings


class ScriptedRuntime:
    def __init__(self, script=None):
        self.script = script
        self.calls = []
        self.runs = []
        self.sessions = {}
        self.cancelled = {}

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "create_session":
            self.sessions[params["id"]] = params
            self.cancelled[params["id"]] = threading.Event()
            return {"model": "lecture/test"}
        if method == "reset":
            self.cancelled[params["session_id"]].clear()
        elif method == "close_session":
            self.sessions.pop(params["session_id"], None)
        return {}

    def cancel(self, session_id):
        self.cancelled[session_id].set()

    def stream(self, method, **params):
        assert method == "run"
        self.runs.append(params)
        if self.script:
            yield from self.script(self, params)
        else:
            yield {"type": "event", "name": "agent.delta", "data": {"text": "Answer"}}
            yield {
                "type": "result",
                "result": {"final_message": "Answer", "steps": 1, "total_tokens": 14},
            }


class StubSettingsPlugin(Plugin):
    id = "settings"
    requires = ()

    def __init__(self, settings):
        self.settings = settings

    def setup(self, ctx):
        ctx.register_service("settings", self.settings)


class StubLLMPlugin(Plugin):
    id = "llm"
    requires = ()

    def setup(self, ctx):
        pass


def _make_agent_runtime(tmp_path, script=None, settings=None):
    llm = FakeLLM()
    llm.runtime = ScriptedRuntime(script)
    settings = settings or FakeSettings()
    runtime = build_runtime(
        load_config(data_dir=tmp_path),
        llm=llm,
        plugins=[StubSettingsPlugin(settings), StubLLMPlugin(), AgentPlugin()],
    )
    runtime.start()
    return runtime, runtime.services.require("agent"), llm.runtime, settings


def test_sessions_use_roles_and_authorized_files(tmp_path):
    runtime, agent, sdk, _ = _make_agent_runtime(tmp_path)
    try:
        source = tmp_path / "source.md"
        source.write_text("complete source")
        profile = AgentProfile(
            name="review",
            role="note_review",
            tools=[AgentTool("read"), AgentTool("grep"), AgentTool("glob")],
        )
        agent.register_agent(profile)
        session = agent.create_session(
            "review",
            agent="review",
            workspace=FileWorkspace({"source.md": source}, readonly=("source.md",)),
        )
        declaration = sdk.sessions[session.id]
        assert declaration["role"] == "note_review"
        assert declaration["files"] == {"source.md": str(source)}
        assert declaration["readonly"] == ["source.md"]
        assert declaration["tools"] == ["read", "grep", "glob"]
        assert agent.list_sessions()[0]["model"] == "lecture/test"
    finally:
        runtime.stop()


def test_runs_keep_sdk_history_and_forward_events(tmp_path):
    runtime, agent, sdk, _ = _make_agent_runtime(tmp_path)
    try:
        session = agent.create_session("chat")
        observed = []
        first = agent.run(session.id, "First question", on_event=observed.append)
        second = agent.run(session.id, "Second question")
        assert first.final_message == second.final_message == "Answer"
        assert first.total_tokens == 14
        assert [frame["instruction"] for frame in sdk.runs] == [
            "First question",
            "Second question",
        ]
        assert all(set(frame) == {"session_id", "instruction"} for frame in sdk.runs)
        assert [event.type for event in observed] == [
            "agent.run.started",
            "agent.delta",
            "agent.run.finished",
        ]
        assert [message["content"] for message in session.messages] == [
            "First question",
            "Answer",
            "Second question",
            "Answer",
        ]
        agent.reset(session.id)
        assert not session.messages
        agent.close(session.id)
        assert agent.get_session(session.id) is None
        assert session.id not in sdk.sessions
    finally:
        runtime.stop()


def test_file_content_is_published_before_callbacks_and_refresh(tmp_path):
    text = "# Formula\n\n$$\nf(x) = \"quoted\" + 'nested'\n$$\n"

    def script(sdk, params):
        yield {
            "type": "event",
            "name": "agent.file",
            "data": {"file": "notes.md", "content": text},
        }
        yield {"type": "result", "result": {"final_message": "Saved", "steps": 2}}

    runtime, agent, sdk, _ = _make_agent_runtime(tmp_path, script)
    try:
        path = tmp_path / "notes.md"
        path.write_text("old")
        session = agent.create_session("writer", files={"notes.md": path})
        seen = []
        result = agent.run(
            session.id,
            "Save",
            on_event=lambda event: (
                seen.append(path.read_text()) if event.type == "agent.file" else None
            ),
        )
        assert not result.error
        assert seen == [text]
        assert path.read_text() == text
    finally:
        runtime.stop()


def test_external_change_is_preserved(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("original")

    def script(sdk, params):
        path.write_text("external change")
        yield {
            "type": "event",
            "name": "agent.file",
            "data": {
                "file": "notes.md",
                "previous": "original",
                "content": "model change",
            },
        }

    runtime, agent, sdk, _ = _make_agent_runtime(tmp_path, script)
    try:
        session = agent.create_session("writer", files={"notes.md": path})
        result = agent.run(session.id, "Edit")
        assert result.error
        assert path.read_text() == "external change"
    finally:
        runtime.stop()


@pytest.mark.parametrize("logical", ["source.md", "unauthorized.md"])
def test_runtime_cannot_publish_readonly_or_unknown_files(tmp_path, logical):
    def script(sdk, params):
        yield {
            "type": "event",
            "name": "agent.file",
            "data": {"file": logical, "content": "overwritten"},
        }

    runtime, agent, _, _ = _make_agent_runtime(tmp_path, script)
    try:
        source = tmp_path / "source.md"
        source.write_text("original")
        session = agent.create_session(
            "reader",
            workspace=FileWorkspace({"source.md": source}, readonly=("source.md",)),
        )
        assert agent.run(session.id, "Read").error
        assert source.read_text() == "original"
        assert not (tmp_path / "unauthorized.md").exists()
    finally:
        runtime.stop()


def test_incomplete_runtime_stream_is_failure(tmp_path):
    def script(sdk, params):
        yield {"type": "event", "name": "agent.delta", "data": {"text": "unfinished"}}

    runtime, agent, _, _ = _make_agent_runtime(tmp_path, script)
    try:
        session = agent.create_session("chat")
        assert agent.run(session.id, "Run").error
        assert not session.runtime_ready
    finally:
        runtime.stop()


def test_cancel_stops_active_runtime_run(tmp_path):
    started = threading.Event()

    def script(sdk, params):
        started.set()
        assert sdk.cancelled[params["session_id"]].wait(3)
        yield {"type": "result", "result": {"error": "cancelled"}}

    runtime, agent, _, _ = _make_agent_runtime(tmp_path, script)
    try:
        session = agent.create_session("chat")
        thread = agent._run_async(session.id, "Run")
        assert started.wait(3)
        assert agent.run(session.id, "Concurrent").error
        agent.cancel(session.id)
        thread.join(3)
        assert not thread.is_alive()
        assert session.cancelled.is_set()
    finally:
        runtime.stop()


def test_agent_ui_actions_remain_usable(tmp_path):
    runtime, agent, _, settings = _make_agent_runtime(tmp_path)
    try:
        state = runtime.ui.state()
        assert "agent:chat" in [page["id"] for page in state["pages"]]
        created = runtime.ui.dispatch_action(
            "agent.new_session", {"name": "My lecture"}
        )
        assert created.ok and created.open_page == "agent:chat"
        session_id = agent.list_sessions()[0]["id"]
        assert runtime.ui.dispatch_action(
            "agent.select_session", {"session_id": session_id}
        ).ok
        assert runtime.ui.dispatch_action("agent.reset", {"session_id": session_id}).ok
        internal = agent.create_session("background", meta={"internal": True})
        assert agent.get_session(internal.id).to_dict()["internal"]
    finally:
        runtime.stop()


def test_run_preserves_failure_when_cancel_fails(tmp_path):
    def fail(sdk, params):
        raise RuntimeError("runtime exited")
        yield

    runtime, agent, sdk, _ = _make_agent_runtime(tmp_path, fail)
    session = agent.create_session("failure")

    def cancel(session_id):
        raise RuntimeError("cancel transport failed")

    sdk.cancel = cancel
    try:
        result = agent.run(session.id, "Run")
        assert result.error == "runtime exited"
        with pytest.raises(RuntimeError, match="cancel transport failed"):
            agent.close(session.id)
        assert agent.get_session(session.id) is None
        assert session.id not in sdk.sessions
    finally:
        runtime.stop()


def test_cleanup_does_not_start_runtime(tmp_path, monkeypatch):
    from agent_runtime.client import RuntimeClient

    client = RuntimeClient(tmp_path)

    def start():
        raise AssertionError("Cleanup restarted the runtime")

    monkeypatch.setattr(client, "_start", start)
    client.cancel("completed")
    client.call("close_session", session_id="completed")
    assert client._process is None


def test_execution_updates_session_model(tmp_path):
    def script(sdk, params):
        yield {
            "type": "event",
            "name": "agent.execution",
            "data": {"metrics": {"model": "resolved-model"}},
        }
        yield {"type": "result", "result": {"final_message": "Answer"}}

    runtime, agent, sdk, _ = _make_agent_runtime(tmp_path, script)
    try:
        session = agent.create_session("chat")
        result = agent.run(session.id, "Question")
        assert not result.error
        assert agent.list_sessions()[0]["model"] == "resolved-model"
    finally:
        runtime.stop()
