from __future__ import annotations

import threading

import pytest

from config import load_config
from core.agent_types import (
    AgentEventType,
    AgentProfile,
    AgentRole,
    AgentTool,
    IpcMethod,
)
from core.plugin import Plugin
from core.runtime import build_runtime
from core.workspace import FileWorkspace
from plugins.agent import AgentPlugin
from tests.support import FakeSettings


class ScriptedRuntime:
    def __init__(self, script=None):
        self.script = script
        self.calls = []
        self.runs = []
        self.sessions = {}
        self.cancelled = {}
        self.transcripts = {}
        self.closed = False

    def call(self, method: IpcMethod, **params):
        self.calls.append((method, params))
        if method == IpcMethod.CREATE_SESSION:
            self.sessions[params["id"]] = params
            self.cancelled[params["id"]] = threading.Event()
            return {"model": "lecture/test"}
        elif method == IpcMethod.CLOSE_SESSION:
            self.sessions.pop(params["session_id"], None)
        return {}

    def cancel(self, session_id):
        self.cancelled[session_id].set()

    def session_transcript(self, session_id):
        return self.transcripts.get(session_id)

    def stream(self, method: IpcMethod, **params):
        assert method == IpcMethod.RUN
        self.runs.append(params)
        if self.script:
            yield from self.script(self, params)
        else:
            yield {"type": "event", "name": "agent.delta", "data": {"text": "Answer"}}
            yield {
                "type": "result",
                "result": {"final_message": "Answer", "steps": 1, "total_tokens": 14},
            }

    def close(self):
        self.closed = True


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
    llm = ScriptedRuntime(script)
    settings = settings or FakeSettings()
    runtime = build_runtime(
        load_config(data_dir=tmp_path),
        llm=llm,
        plugins=[StubSettingsPlugin(settings), StubLLMPlugin(), AgentPlugin()],
    )
    runtime.start()
    return runtime, runtime.services.require("agent"), llm, settings


def test_ordered_followups_and_review_approval(tmp_path):
    def script(sdk, params):
        yield {
            "type": "result",
            "result": {
                "final_message": "Stopped",
                "review_passed": True,
            },
        }

    runtime, agent, sdk, _ = _make_agent_runtime(tmp_path, script)
    try:
        session = agent.create_session("writer")
        result = agent.run(session.id, "Stop reviewing", follow_ups=["Keep the notes"])
        assert sdk.runs[0]["instruction"] == "Stop reviewing"
        assert sdk.runs[0]["follow_ups"] == ["Keep the notes"]
        assert result.review_passed is True
    finally:
        runtime.stop()


def test_sessions_use_roles_and_authorized_files(tmp_path):
    runtime, agent, sdk, _ = _make_agent_runtime(tmp_path)
    try:
        source = tmp_path / "source.md"
        source.write_text("complete source")
        profile = AgentProfile(
            name="review",
            role=AgentRole.NOTE_REVIEW,
            tools=[AgentTool("read"), AgentTool("grep"), AgentTool("glob")],
        )
        agent.register_agent(profile)
        session = agent.create_session(
            "review",
            agent="review",
            workspace=FileWorkspace({"source.md": source}, readonly=("source.md",)),
        )
        declaration = sdk.sessions[session.id]
        assert declaration["role"] == AgentRole.NOTE_REVIEW
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
        agent.close(session.id)
        assert agent.get_session(session.id) is None
        assert session.id not in sdk.sessions
    finally:
        runtime.stop()


@pytest.mark.parametrize(
    "payload",
    [
        {"finished": False},
        {"event": AgentEventType.RUN_READY},
        {"event": AgentEventType.DELTA},
        {"finished": True},
    ],
)
def test_reviewer_events_preserve_run(tmp_path, payload):
    def script(sdk, params):
        yield {
            "type": "event",
            "name": "agent.reviewer",
            "data": {
                "role": "note_review",
                **payload,
                "reviewer_id": "writer-review",
                "text": "Review text",
            },
        }
        yield {"type": "result", "result": {"final_message": "Notes complete"}}

    runtime, agent, _, _ = _make_agent_runtime(tmp_path, script)
    try:
        session = agent.create_session("writer")
        observed = []
        result = agent.run(session.id, "Write notes", on_event=observed.append)
        assert result.error is None
        assert result.final_message == "Notes complete"
        forwarded = next(item for item in observed if item.type == "agent.reviewer")
        for name, value in payload.items():
            assert forwarded.data[name] is value
        assert forwarded.data["reviewer_id"] == "writer-review"
    finally:
        runtime.stop()


def test_file_content_is_published_before_callbacks_and_refresh(tmp_path, monkeypatch):
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
        from contextlib import contextmanager

        guarded = []

        @contextmanager
        def write_guard(target):
            assert target == path
            guarded.append(True)
            try:
                yield
            finally:
                guarded.pop()

        path = tmp_path / "notes.md"
        path.write_text("old")
        write = FileWorkspace.write

        def guarded_write(workspace, logical, content):
            assert guarded
            return write(workspace, logical, content)

        monkeypatch.setattr(FileWorkspace, "write", guarded_write)
        session = agent.create_session("writer", files={"notes.md": path})
        seen = []
        result = agent.run(
            session.id,
            "Save",
            on_event=lambda event: (
                seen.append((path.read_text(), bool(guarded)))
                if event.type == "agent.file"
                else None
            ),
            write_guard=write_guard,
        )
        assert not result.error
        assert seen == [(text, False)]
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
        thread = threading.Thread(target=agent.run, args=(session.id, "Run"))
        thread.start()
        assert started.wait(3)
        assert agent.run(session.id, "Concurrent").error
        agent.cancel(session.id)
        thread.join(3)
        assert not thread.is_alive()
        assert session.cancelled.is_set()
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


def test_runtime_rejects_string_method(tmp_path):
    from agent_runtime.client import RuntimeClient

    client = RuntimeClient(tmp_path)
    with pytest.raises(TypeError, match="IpcMethod"):
        client.call("info")
    assert client._process is None


def test_cleanup_does_not_start_runtime(tmp_path, monkeypatch):
    from agent_runtime.client import RuntimeClient

    client = RuntimeClient(tmp_path)

    def start():
        raise AssertionError("Cleanup restarted the runtime")

    monkeypatch.setattr(client, "_start", start)
    client.cancel("completed")
    client.call(IpcMethod.CLOSE_SESSION, session_id="completed")
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


def test_message_does_not_wait_for_run_lock(tmp_path):
    started = threading.Event()
    release = threading.Event()

    def script(sdk, params):
        yield {
            "type": "event",
            "name": "agent.run.ready",
            "data": {"session_id": params["session_id"]},
        }
        started.set()
        assert release.wait(3)
        yield {"type": "result", "result": {"final_message": "continued"}}

    runtime, agent, sdk, _ = _make_agent_runtime(tmp_path, script)
    original = sdk.call
    messages = []

    def call(method, **params):
        if method == IpcMethod.MESSAGE:
            messages.append(params)
            release.set()
            return {"accepted": True}
        return original(method, **params)

    sdk.call = call
    session = agent.create_session("writer")
    observed = []
    thread = threading.Thread(
        target=lambda: agent.run(session.id, "start", on_event=observed.append)
    )
    thread.start()
    try:
        assert started.wait(3)
        assert agent.message(session.id, "first\nsecond") == {"accepted": True}
        thread.join(3)
        assert not thread.is_alive()
        assert messages == [{"session_id": session.id, "message": "first\nsecond"}]
        assert any(
            event.type == "agent.run.ready" and event.session_id == session.id
            for event in observed
        )
        assert not session.cancelled.is_set()
        assert agent.message("bun-owned-reviewer", "review detail")["accepted"]
    finally:
        release.set()
        thread.join(3)
        runtime.stop()


def test_message_rejects_missing_admission_and_closed_runtime(tmp_path, monkeypatch):
    from agent_runtime.client import RuntimeClient

    runtime, agent, sdk, _ = _make_agent_runtime(tmp_path)
    try:
        assert not agent.message("unknown", "hello")["accepted"]
        assert not agent.message("unknown", " \n ")["accepted"]
        client = RuntimeClient(tmp_path)
        monkeypatch.setattr(
            client, "_start", lambda: pytest.fail("Message started a process")
        )
        agent._runtime = client
        result = agent.message("completed", "hello")
        assert not result["accepted"]
        assert "closed" in result["reason"]
        assert client._process is None
    finally:
        runtime.stop()


def test_reopen_uses_strict_native_operation(tmp_path):
    runtime, agent, sdk, _ = _make_agent_runtime(tmp_path)
    session = agent.create_session("writer", role=AgentRole.NOTE_WRITER, tools=[])
    values = dict(
        name="writer",
        workspace=session.workspace,
        system_prompt="system",
        role=AgentRole.NOTE_WRITER,
        tools=[],
        meta={},
    )
    try:
        with pytest.raises(RuntimeError, match="still live"):
            agent.reopen(session.id, **values)
        agent.close(session.id)
        original = sdk.call

        def missing(method, **params):
            if method == IpcMethod.REOPEN_SESSION:
                raise RuntimeError("Saved transcript is missing")
            return original(method, **params)

        sdk.call = missing
        with pytest.raises(RuntimeError, match="transcript"):
            agent.reopen(session.id, **values)
        assert agent.get_session(session.id) is None

        def reopen(method, **params):
            if method == IpcMethod.REOPEN_SESSION:
                sdk.sessions[params["id"]] = params
                sdk.cancelled[params["id"]] = threading.Event()
                return {"model": "restored-model"}
            return original(method, **params)

        sdk.call = reopen
        restored = agent.reopen(session.id, **values)
        assert restored.id == session.id
        assert restored.model == "restored-model"
        assert not restored.cancelled.is_set()
    finally:
        runtime.stop()


def test_can_resume_reads_saved_transcript_through_runtime(tmp_path):
    from agent_runtime.client import RuntimeClient

    runtime, agent, _sdk, _ = _make_agent_runtime(tmp_path)
    try:
        live = agent.create_session("writer")
        client = RuntimeClient(tmp_path)
        agent._runtime = client
        assert agent.can_resume("") == (False, "agent session is unknown")
        assert agent.can_resume("writer") == (
            False,
            "saved session transcript is unavailable",
        )
        transcript = tmp_path / "writer.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        (tmp_path / "session-writer.path").write_text(str(transcript), encoding="utf-8")
        assert agent.can_resume("writer") == (True, "")
        (tmp_path / "session-writer.path").write_text("", encoding="utf-8")
        assert not agent.can_resume("writer")[0]
        transcript.unlink()
        (tmp_path / "session-writer.path").write_text(str(transcript), encoding="utf-8")
        assert not agent.can_resume("writer")[0]
        assert agent.can_resume(live.id) == (False, "agent session is still live")
        assert client._process is None
    finally:
        runtime.stop()
