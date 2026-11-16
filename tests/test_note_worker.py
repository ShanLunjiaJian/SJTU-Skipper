"""Exercise note workers with a real HTTP connection and fresh interpreters."""

import json
import queue
import threading
import os
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from core.events import EventBus
from core.streams import StreamHub
from llm.service import LLMService
from llm.types import ProviderConfig
from plugins.agent.plugin import AgentService
from plugins.notes_pipeline.prompts import NOTE_HEADERS
from plugins.agent.workspace import FileWorkspace
from plugins.notes_pipeline.plugin import NotesPipelinePlugin
from tests.support import FakeSettings


@contextmanager
def endpoint(respond):
    requests = queue.Queue()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.put((body, dict(self.headers)))
            response = respond(body)
            encoded = json.dumps(response).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def response(content="done", tool_calls=None):
    return {
        "id": "worker-test",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls or [],
                },
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
    }


@contextmanager
def pipeline_context(tmp_path, url):
    bus = EventBus()
    hub = StreamHub(emit=bus.emit)
    settings = FakeSettings(
        {"notes.concurrency": 4, "instant-summarize.enabled": False}
    )
    config = ProviderConfig(
        base_url=url,
        api_key="test-secret",
        model="test-model",
        timeout=120,
        max_retries=0,
        extra_headers={"X-Worker-Test": "preserved"},
        extra_body={"reasoning_effort": "high"},
    )
    llm = LLMService(config_provider=lambda _: config)
    ctx = SimpleNamespace(
        data_dir=tmp_path,
        streams=hub,
        events=bus,
        llm=llm,
        meta=SimpleNamespace(render=lambda: "test metadata"),
        logger=__import__("logging").getLogger("test.note_worker"),
        ui=SimpleNamespace(
            refresh=lambda reason: None, register_task_provider=lambda *a, **k: None
        ),
        register_service=lambda *a: None,
    )
    agent = AgentService(ctx, "test-model", 10, 200000, "test system")
    ctx.require = lambda name: settings if name == "settings" else agent
    ctx.service = lambda name: llm if name == "llm" else None
    pipeline = NotesPipelinePlugin()
    pipeline.setup(ctx)
    try:
        yield pipeline, agent, llm, bus
    finally:
        pipeline.teardown()
        llm.close()


def test_history_config_and_usage(tmp_path):
    call_count = 0

    def respond(body):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return response(
                tool_calls=[
                    {
                        "id": "private",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": json.dumps({"file": "source.md"}),
                        },
                    }
                ]
            )
        return response("first complete" if call_count == 2 else "revision complete")

    with (
        endpoint(respond) as (url, requests),
        pipeline_context(tmp_path, url) as (pipeline, agent, llm, bus),
    ):
        source = tmp_path / "source.md"
        source.write_text("Original source with all conditions.\n" * 20)
        workspace = FileWorkspace({"source.md": source}, readonly=("source.md",))
        session = agent.create_session(
            "writer",
            workspace=workspace,
            tools=["info", "read"],
            max_history_chars=1,
            llm_extra={"custom_parameter": "kept", "reasoning_effort": "max"},
            max_tokens=1234,
            extra_instructions=("Keep all source facts.",),
        )
        pipeline._state("lecture")
        events = []
        bus.on("agent.message", lambda event: events.append(event))
        first = pipeline._agent_run("lecture", session.id, "Write all details.")
        assert first.final_message == "first complete"
        private_files = [
            name
            for name in session.workspace.names
            if session.workspace.is_hidden(name)
        ]
        assert private_files
        second = pipeline._agent_run(
            "lecture", session.id, "Revise with these comments."
        )
        assert second.final_message == "revision complete"
        bodies = [requests.get(timeout=1) for _ in range(3)]
        final, headers = bodies[-1]
        assert final["model"] == "test-model"
        assert final["max_tokens"] == 1234
        assert final["reasoning_effort"] == "max"
        assert final["custom_parameter"] == "kept"
        assert headers["X-Worker-Test"] == "preserved"
        assert headers["Authorization"] == "Bearer test-secret"
        messages = final["messages"]
        assert any(
            "Write all details." in message.get("content", "") for message in messages
        )
        assert any(message.get("content") == "first complete" for message in messages)
        assert any(
            "Revise with these comments." in message.get("content", "")
            for message in messages
        )
        assert all(name in session.workspace.names for name in private_files)
        bus.flush()
        assert len(events) == 3
        assert llm.usage_snapshot()["test-model"]["requests"] == 3
        traces = list((tmp_path / "llm").rglob("*.json"))
        assert len(traces) == 3
        assert all(
            json.loads(path.read_text())["pid"] != os.getpid() for path in traces
        )
        assert not pipeline.workers
        agent.close(session.id)


@pytest.mark.parametrize("regenerate", [False, True])
def test_reset_kills_blocked_http(tmp_path, regenerate):
    release = threading.Event()

    def respond(body):
        release.wait(30)
        return response("obsolete generation")

    try:
        with (
            endpoint(respond) as (url, requests),
            pipeline_context(tmp_path, url) as (pipeline, agent, llm, bus),
        ):
            pipeline._state("lecture")
            errors = queue.Queue()
            threads = []
            for role in ("writer", "coverage", "correctness"):
                session = agent.create_session(role, tools=[])

                def execute(session=session):
                    try:
                        pipeline._agent_run(
                            "lecture", session.id, "Wait for HTTP response."
                        )
                    except Exception as exc:
                        errors.put(exc)
                    finally:
                        agent.close(session.id)

                thread = threading.Thread(target=execute)
                thread.start()
                threads.append(thread)
            for _ in threads:
                requests.get(timeout=10)
            with pipeline.condition:
                processes = [process for _, process in pipeline.workers.values()]
            assert len(processes) == 3
            started = time.monotonic()
            assert pipeline.reset("lecture", regenerate=regenerate)
            assert time.monotonic() - started < 3
            assert not release.is_set()
            for thread in threads:
                thread.join(timeout=2)
                assert not thread.is_alive()
            assert all(process.returncode is not None for process in processes)
            assert not pipeline.workers
            assert not pipeline.agent_tasks
            assert errors.qsize() == 3
            assert pipeline._state("lecture")["deleted"] is not regenerate
            assert not (
                tmp_path / "sessions" / "lecture" / "notes" / "note_knowledge.md"
            ).exists()
            release.set()
    finally:
        release.set()


def test_regenerate_publishes_after_kill(tmp_path):
    release = threading.Event()
    call_count = 0
    lock = threading.Lock()

    def respond(body):
        nonlocal call_count
        with lock:
            call_count += 1
            number = call_count
        if number == 1:
            release.wait(30)
            return response("obsolete generation")
        if number == 2:
            return response(
                tool_calls=[
                    {
                        "id": f"write-{index}",
                        "type": "function",
                        "function": {
                            "name": "write",
                            "arguments": json.dumps(
                                {
                                    "file": name,
                                    "text": "# Notes\n\nFresh generation with source details.\n",
                                }
                            ),
                        },
                    }
                    for index, name in enumerate(NOTE_HEADERS)
                ]
            )
        return response("complete")

    try:
        with (
            endpoint(respond) as (url, requests),
            pipeline_context(tmp_path, url) as (pipeline, agent, llm, bus),
        ):
            pipeline.settings.values["summarize.review_enabled"] = False
            updates = queue.Queue()
            pipeline.ctx.ui.refresh = updates.put
            lecture = pipeline.ctx.streams.start_session()
            lecture.transcript.append(
                "Definition and proof with all conditions.", start=0, end=10
            )
            pipeline.ctx.streams.end_session()
            assert pipeline.start(lecture.session_id)
            requests.get(timeout=10)
            with pipeline.condition:
                old_processes = [process for _, process in pipeline.workers.values()]
            assert pipeline.reset(lecture.session_id, regenerate=True)
            assert all(process.returncode is not None for process in old_processes)
            assert not release.is_set()
            deadline = time.monotonic() + 10
            while (
                updates.get(timeout=max(0.01, deadline - time.monotonic()))
                != "notes.published"
            ):
                assert time.monotonic() < deadline, pipeline.status(lecture.session_id)
            assert pipeline.status(lecture.session_id)["phase"] == "published"
            notes = tmp_path / "sessions" / lecture.session_id / "notes"
            for name in NOTE_HEADERS:
                assert (
                    "Fresh generation with source details."
                    in (notes / name).read_text()
                )
                assert "obsolete" not in (notes / name).read_text()
            release.set()
    finally:
        release.set()


def test_writer_draft_visible_before_completion(tmp_path):
    release = threading.Event()
    calls = 0

    def respond(body):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response(tool_calls=[{"id": "chapter", "type": "function", "function": {"name": "append", "arguments": json.dumps({"file": "note_knowledge.md", "text": "## First chapter\n\nComplete first theorem."})}}])
        release.wait(15)
        return response("complete")

    try:
        with endpoint(respond) as (url, requests), pipeline_context(tmp_path, url) as (pipeline, agent, llm, bus):
            from plugins.notes_pipeline.prompts import NOTE_HEADERS, SOURCE_FILENAME
            directory = tmp_path / "private"
            directory.mkdir()
            files = {name: directory / name for name in (*NOTE_HEADERS, SOURCE_FILENAME)}
            for name, path in files.items():
                path.write_text(NOTE_HEADERS.get(name, "Original source"))
            session = agent.create_session("writer", workspace=FileWorkspace(files), tools=["append"], meta={"role": "writer"})
            pipeline._state("lecture")
            results = queue.Queue()
            thread = threading.Thread(target=lambda: results.put(pipeline._agent_run("lecture", session.id, "Write chapters.")))
            thread.start()
            try:
                requests.get(timeout=10)
                requests.get(timeout=10)
                assert thread.is_alive()
                text = (tmp_path / "sessions" / "lecture" / "notes" / "note_knowledge.md").read_text()
                assert "Complete first theorem." in text
                assert "未审阅" in text
                assert pipeline.status("lecture")["phase"] != "published"
            finally:
                release.set()
                thread.join(5)
                agent.close(session.id)
            assert not results.get(timeout=1).error
    finally:
        release.set()
