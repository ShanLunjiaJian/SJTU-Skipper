import json

import pytest

from config import load_config
from core.runtime import build_runtime
from plugins.agent import AgentPlugin
from plugins.note_check_stc import NoteCheckSTCPlugin
from plugins.note_check_stc.plugin import ENABLED_KEY, TOOL_NAME
from plugins.notes import NotesPlugin
from plugins.reader import ReaderPlugin
from plugins.settings import SettingsPlugin
from test_agent import CallbackRuntime, StubLLMPlugin


@pytest.fixture
def stc_runtime(tmp_path):
    sdk = CallbackRuntime()
    runtime = build_runtime(
        load_config(data_dir=tmp_path),
        llm=sdk,
        plugins=[
            SettingsPlugin(),
            StubLLMPlugin(),
            AgentPlugin(),
            ReaderPlugin(),
            NotesPlugin(),
            NoteCheckSTCPlugin(),
        ],
    )
    runtime.start()
    try:
        yield runtime, runtime.services.require("agent"), sdk
    finally:
        runtime.stop()


def review_session(agent, tmp_path):
    note = tmp_path / "note.md"
    note.write_text("显著。\n", encoding="utf-8")
    source = tmp_path / "source.md"
    source.write_text("来源．\n", encoding="utf-8")
    session = agent.create_session(
        "writer",
        role="note_writer",
        files={"note_knowledge.md": note, "source.md": source},
        meta={
            "review": {
                "role": "note_review",
                "targets": ["note_knowledge.md"],
                "sources": ["source.md"],
            }
        },
    )
    return session, {"note_knowledge.md": str(note), "source.md": str(source)}


def callback(sdk, session, workspace, kind, name, **arguments):
    return sdk.callback_handler(
        {
            "session_id": session.id + "-review",
            "role": "note_review",
            "kind": kind,
            "name": name,
            "arguments": {"workspace": workspace, **arguments},
        }
    )


def test_check_gate_requires_success_on_note(stc_runtime, tmp_path):
    _, agent, sdk = stc_runtime
    session, workspace = review_session(agent, tmp_path)
    hook = lambda calls: callback(
        sdk,
        session,
        workspace,
        "completion_hook",
        "note_check_stc/check",
        text="{}",
        tool_calls=calls,
    )
    tool = lambda path: callback(
        sdk, session, workspace, "tool", TOOL_NAME, parameters={"path": path}
    )
    assert hook([])["continue"]
    denied = tool("../note.md")
    assert denied["isError"]
    assert hook([{"name": TOOL_NAME, "result": denied}])["continue"]
    source = tool("source.md")
    assert hook([{"name": TOOL_NAME, "result": source}])["continue"]
    result = tool("note_knowledge.md")
    assert not result["isError"]
    assert json.loads(result["content"][0]["text"]) == result["details"]
    assert {issue["rule"] for issue in result["details"]["issues"]} == {"G5", "G17"}
    assert hook([{"name": TOOL_NAME, "result": result}]) is None
    assert (tmp_path / "note.md").read_text(encoding="utf-8") == "显著。\n"


def test_disable_preserves_live_callback(stc_runtime, tmp_path):
    runtime, agent, sdk = stc_runtime
    session, workspace = review_session(agent, tmp_path)
    settings = runtime.services.require("settings")
    settings.set(ENABLED_KEY, False)
    assert runtime.bus.flush()
    fresh = agent.create_session("disabled", role="note_writer")
    assert sdk.sessions[fresh.id]["extensions"]["prompts"] == []
    result = callback(
        sdk,
        session,
        workspace,
        "tool",
        TOOL_NAME,
        parameters={"path": "note_knowledge.md"},
    )
    assert result["details"]["errors"] > 0
    settings.set(ENABLED_KEY, True)
    assert runtime.bus.flush()
    restored = agent.create_session("restored", role="note_review")
    assert [
        tool["name"] for tool in sdk.sessions[restored.id]["extensions"]["tools"]
    ] == [TOOL_NAME]
    runtime.plugins.unload("note_check_stc")
    plain = agent.create_session("unloaded", role="note_review")
    assert sdk.sessions[plain.id]["extensions"]["tools"] == []


def test_tool_is_review_only(stc_runtime, tmp_path):
    _, agent, sdk = stc_runtime
    session, _ = review_session(agent, tmp_path)
    writer = sdk.sessions[session.id]["extensions"]
    assert writer["tools"] == []
    assert [tool["name"] for tool in writer["reviewer"]["tools"]] == [TOOL_NAME]
    chunk = agent.create_session("chunk", role="note_chunk")
    assert sdk.sessions[chunk.id]["extensions"]["tools"] == []
    plain = agent.create_session("plain")
    assert sdk.sessions[plain.id]["extensions"]["prompts"] == []
