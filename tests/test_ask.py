"""Ask plugin: intensities, tool parameters, streaming and fold persistence."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from config import load_config
from core.agent_types import AgentEvent
from core.plugin import Plugin, PluginContext
from core.runtime import build_runtime
from plugins.agent import AgentPlugin
from plugins.ask.plugin import AskPlugin, _Live
from plugins.reader.plugin import ReaderPlugin
from plugins.settings.plugin import SettingsPlugin

from tests.test_agent import ScriptedRuntime


class StubLLM(Plugin):
    id = "llm"
    requires = ("settings",)

    def setup(self, ctx: PluginContext) -> None:
        pass


def _wait_until(predicate, timeout: float = 8.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _build(tmp_path: Path, script) -> tuple:
    cfg = load_config(data_dir=tmp_path)
    llm = ScriptedRuntime(script)
    runtime = build_runtime(
        cfg,
        plugins=[
            SettingsPlugin(),
            StubLLM(),
            AgentPlugin(),
            ReaderPlugin(),
            AskPlugin(),
        ],
        llm=llm,
    )
    runtime.start()
    return runtime, llm


def _open_note(runtime, tmp_path: Path) -> Path:
    notes = tmp_path / "sessions" / "ask-session" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    note = notes / "note_knowledge.md"
    note.write_text("# 笔记\n\n第一段：这是定义。\n\n第二段：这是例题。\n", "utf-8")
    runtime.services.require("reader").show(
        title="测试笔记",
        docs=[{"key": "knowledge", "label": "知识", "path": str(note)}],
        page_key="ask-session",
        owner="test",
    )
    return note


def test_instant_ask_answers_and_persists_fold(tmp_path: Path) -> None:
    snapshots = []

    def script(sdk, params):
        yield {
            "type": "event",
            "name": "agent.execution",
            "data": {
                "metrics": {"model": "test-model", "output_tokens": 3},
                "block": {
                    "id": "1:0",
                    "kind": "text",
                    "status": "completed",
                    "text": "Answer",
                },
            },
        }
        qa_id = events[0]["id"]
        snapshots.append(runtime.services.require("ask").activity(str(note), qa_id))
        pages = runtime.ui.state()["page_content"]

        def activities(blocks):
            for block in blocks:
                if block.get("type") == "qa":
                    yield block.get("activity")
                for tab in block.get("tabs", []):
                    yield from activities(tab.get("blocks", []))

        assert snapshots[-1] in list(activities(next(iter(pages.values()))))
        yield {"type": "result", "result": {"final_message": "Answer", "steps": 1}}

    runtime, llm = _build(tmp_path, script)
    events: list[dict] = []
    runtime.bus.on("ui.notify", lambda event: events.append(dict(event.get("payload"))))
    try:
        note = _open_note(runtime, tmp_path)
        result = runtime.ui.dispatch_action(
            "ask.ask",
            {
                "path": str(note),
                "selection": "第一段：这是定义。",
                "question": "这一段在说什么？",
                "intensity": "instant",
            },
        )
        assert result.ok, result.message
        assert _wait_until(lambda: '"status": "done"' in note.read_text("utf-8")), (
            note.read_text("utf-8")
        )

        text = note.read_text("utf-8")
        assert "Answer" in text
        assert '"intensity": "instant"' in text
        assert '"collapsed": true' in text
        assert text.index("这一段在说什么？") < text.index("第二段：这是例题。")

        declaration = next(
            params for method, params in llm.calls if method == "create_session"
        )
        assert declaration["role"] == "ask_instant"
        assert declaration["tools"] == []
        prompt = llm.runs[0]["instruction"]
        assert "第一段：这是定义。" in prompt
        assert "第二段：这是例题。" in prompt

        assert _wait_until(lambda: any(item.get("status") == "done" for item in events))
        final = next(item for item in events if item.get("status") == "done")
        assert final["collapsed"] is True
        assert "answer_html" in final
        activity = final["activity"]
        assert activity["id"] == f"qa-{final['id']}"
        assert activity["agents"][0]["status"] == "completed"
        assert activity["agents"][0]["blocks"][0]["text"] == "Answer"
        assert events[0]["activity"]["agents"][0]["blocks"] == []
        assert snapshots[0]["agents"][0]["model"] == "test-model"
        assert snapshots[0]["agents"][0]["role"] == "qa"
        assert _wait_until(
            lambda: (
                runtime.services.require("ask").activity(str(note), final["id"]) is None
            )
        )
    finally:
        runtime.stop()


def test_research_ask_reads_complete_snapshot(tmp_path: Path) -> None:
    snapshots = []

    def script(sdk, params):
        declaration = sdk.sessions[params["session_id"]]
        snapshots.append(
            {
                name: Path(path).read_text()
                for name, path in declaration["files"].items()
            }
        )
        assert declaration["role"] == "ask_research"
        assert declaration["tools"] == ["read", "grep", "glob"]
        assert set(declaration["readonly"]) == set(declaration["files"])
        yield {
            "type": "event",
            "name": "agent.tool_call",
            "data": {"name": "grep", "arguments": {"pattern": "condition"}},
        }
        yield {
            "type": "event",
            "name": "agent.tool_result",
            "data": {"name": "grep", "result": "condition"},
        }
        yield {
            "type": "result",
            "result": {
                "final_message": "The source includes every condition.",
                "steps": 2,
            },
        }

    runtime, sdk = _build(tmp_path, script)
    try:
        note = _open_note(runtime, tmp_path)
        source = note.parent.parent / "lecture_source.md"
        source.write_text("All conditions\n" * 1000)
        result = runtime.ui.dispatch_action(
            "ask.ask",
            {
                "path": str(note),
                "selection": "第一段：这是定义。",
                "question": "What are the conditions?",
                "intensity": "research",
            },
        )
        assert result.ok
        assert _wait_until(lambda: '"status": "done"' in note.read_text())
        assert "every condition" in note.read_text()
        assert snapshots[0]["lecture_source.md"] == "All conditions\n" * 1000
        assert "note_knowledge.md" in snapshots[0]
        assert not sdk.sessions
    finally:
        runtime.stop()


def test_live_accumulates_execution() -> None:
    plugin = SimpleNamespace(ctx=SimpleNamespace(ui=Mock(), logger=Mock()))
    with patch("plugins.ask.plugin.time.monotonic", return_value=1.0):
        live = _Live(plugin, "/note.md", "question")
        initial = plugin.ctx.ui.notify.call_args.args[1]
        for data in (
            {"metrics": {"model": "model", "context_tokens": 123}},
            {
                "block": {
                    "id": "thought",
                    "kind": "thinking",
                    "status": "streaming",
                    "text": "Consider",
                }
            },
            {
                "block": {
                    "id": "tool",
                    "kind": "tool",
                    "status": "running",
                    "name": "read",
                    "arguments": {"path": "note.md"},
                }
            },
            {
                "block": {
                    "id": "thought",
                    "kind": "thinking",
                    "status": "completed",
                    "text": "Consider all conditions",
                }
            },
            {
                "metrics": {"tool_seconds": 2},
                "block": {
                    "id": "tool",
                    "kind": "tool",
                    "status": "completed",
                    "name": "read",
                    "result": "source",
                },
            },
            {
                "block": {
                    "id": "answer",
                    "kind": "text",
                    "status": "streaming",
                    "text": "Answer",
                }
            },
        ):
            live.on_event(AgentEvent("agent.execution", "session", data=data))
        live.on_event(
            AgentEvent("agent.reasoning", "session", data={"text": "duplicate"})
        )
        assert plugin.ctx.ui.notify.call_count == 1
        live.publish(force=True)
        pending = plugin.ctx.ui.notify.call_args.args[1]
        agent = pending["activity"]["agents"][0]
        assert agent["context_tokens"] == 123
        assert agent["tool_seconds"] == 2
        assert [block["id"] for block in agent["blocks"]] == [
            "thought",
            "tool",
            "answer",
        ]
        assert agent["blocks"][0]["text"] == "Consider all conditions"
        assert agent["blocks"][1]["result"] == "source"
        live.finish(status="error", answer="Failure", elapsed=2)
        final = plugin.ctx.ui.notify.call_args.args[1]
    assert initial["activity"]["agents"][0]["blocks"] == []
    assert agent["status"] == "running"
    assert agent["blocks"][-1]["status"] == "streaming"
    assert final["activity"]["agents"][0]["status"] == "failed"
    assert final["activity"]["agents"][0]["blocks"][-1]["status"] == "failed"
    assert final["answer"] == "Failure"
    assert final["collapsed"] is False
