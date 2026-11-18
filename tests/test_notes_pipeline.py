from plugins.notes_pipeline.material import chunk_material

import queue
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from core.streams import StreamHub, StreamKind
from core.agent_types import (
    AgentEventType,
    AgentRole,
    AgentToolName,
    ExecutionBlockKind,
    ExecutionBlockStatus,
    ExecutionRole,
    ExecutionStatus,
    ReviewerEventType,
    ReviewStatus,
)
from plugins.notes_pipeline.types import (
    ChunkState,
    ContinuationStage,
    GenerationState,
    NotePhase,
    NotePublication,
    NoteTaskKind,
)
from core.ui import BlockType
from plugins.notes_pipeline.plugin import NotesPipelinePlugin
from plugins.notes_pipeline.prompts import NOTE_HEADERS, SOURCE_FILENAME
from tests.support import FakeSettings


def test_visual_gap_survives_reload(tmp_path):
    import json
    from core.events import Event
    from plugins.notes_pipeline.material import collect_material, render

    hub = StreamHub()
    session = hub.start_session()
    root = tmp_path / "sessions" / session.session_id
    root.mkdir(parents=True)
    detail = {"code": "thinking_loop", "message": "Repeated board", "attempts": 3}
    (root / "visual.jsonl").write_text(
        json.dumps({"start": 5, "end": 10, "error": detail}) + "\n"
    )
    ctx = SimpleNamespace(data_dir=tmp_path, streams=hub)
    source = render(collect_material(ctx, session.session_id))
    assert "[5.000s-10.000s] 板书识别缺口" in source
    assert "Repeated board" in source
    assert list(session.visual) == []
    for _ in range(2):
        pipeline = NotesPipelinePlugin()
        pipeline.ctx = ctx
        pipeline.sessions = {}
        pipeline.condition = threading.Condition()
        pipeline._event(
            Event(
                "stream.sealed",
                {
                    "session_id": session.session_id,
                    "stream": StreamKind.VISUAL,
                    "error": {
                        "start": 5,
                        "end": 10,
                        "error": detail["message"],
                        "code": detail["code"],
                        "attempts": 3,
                    },
                },
                0,
                0,
            )
        )
        assert len(pipeline._state(session.session_id)["errors"]) == 1


def test_gap_reaches_chunk_and_final_source(tmp_path):
    import json

    sources = []

    def run(session, instruction):
        sources.append(
            {
                name: session.workspace.text(name)
                for name in session.workspace.names
                if name in ("source.md", SOURCE_FILENAME)
            }
        )
        return write_notes(session, "Known theorem")

    with note_pipeline(tmp_path, run) as env:
        session = env.hub.start_session()
        root = tmp_path / "sessions" / session.session_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "visual.jsonl").write_text(
            json.dumps(
                {
                    "start": 2,
                    "end": 4,
                    "error": {
                        "code": "thinking_loop",
                        "message": "Repeated board",
                        "attempts": 3,
                    },
                }
            )
            + "\n"
        )
        session.transcript.append("Known theorem", start=0, end=10)
        env.hub.end_session()
        env.hub.seal_text(session.session_id, StreamKind.TRANSCRIPT, 10, final=True)
        env.hub.seal_text(session.session_id, StreamKind.VISUAL, 10, final=True)
        assert env.pipeline.finalize(session.session_id)
        env.wait_until(
            lambda: (
                env.pipeline.status(session.session_id)["phase"] == NotePhase.PUBLISHED
            )
        )
        assert any("板书识别缺口" in entry.get("source.md", "") for entry in sources)
        final = env.pipeline.source_path(session.session_id).read_text()
        assert "[2.000s-4.000s] 板书识别缺口" in final
        assert "Known theorem" in final


def test_fill_gap_time_shift(tmp_path):
    import json
    from plugins.notes_pipeline.material import collect_material, render

    primary = tmp_path / "sessions" / "primary"
    fill = tmp_path / "sessions" / "fill"
    primary.mkdir(parents=True)
    fill.mkdir()
    (primary / "meta.json").write_text(json.dumps({"started_at": 100}))
    (fill / "meta.json").write_text(
        json.dumps({"meta": {"fill_for": "primary", "media_begin": 120}})
    )
    (fill / "visual.jsonl").write_text(
        json.dumps(
            {
                "start": 2,
                "end": 4,
                "error": {
                    "code": "thinking_loop",
                    "message": "Repeated board",
                    "attempts": 3,
                },
            }
        )
        + "\n"
    )
    records = collect_material(
        SimpleNamespace(data_dir=tmp_path, streams=StreamHub()), "primary"
    )
    assert records[0]["origin"] == "fill"
    assert "[22.000s-24.000s] 板书识别缺口" in render(records)


def test_execution_blocks_and_review_invalidation():
    import json
    from core.events import Event
    from plugins.summarize.plugin import note_log_blocks

    pipeline = object.__new__(NotesPipelinePlugin)
    pipeline.condition = threading.Condition()
    pipeline.sessions = {}
    task = {
        "id": "notes-agent:writer",
        "label": "Writer",
        "agent_step": 0,
        "agents": {
            "main": pipeline._execution_agent(
                "main", ExecutionRole.WRITER, "Writer", "model"
            )
        },
    }
    pipeline.agent_tasks = {"writer": task}
    pipeline.ctx = SimpleNamespace(ui=SimpleNamespace(refresh=lambda reason: None))

    def emit(name, **payload):
        pipeline._agent_event(Event(name, {"session_id": "writer", **payload}, 0, 0))

    thinking = {
        "id": "thinking",
        "kind": ExecutionBlockKind.THINKING,
        "text": "Reason",
        "status": ExecutionBlockStatus.STREAMING,
    }
    tool = {
        "id": "tool",
        "kind": ExecutionBlockKind.TOOL,
        "name": "read",
        "arguments": {"path": "note_knowledge.md"},
        "status": ExecutionBlockStatus.RUNNING,
    }
    emit(AgentEventType.EXECUTION, block=thinking, metrics={"context_tokens": 12})
    emit(AgentEventType.EXECUTION, block=tool)
    full_text = "Full reasoning\n" * 1000
    completed = {
        **thinking,
        "text": full_text,
        "status": ExecutionBlockStatus.COMPLETED,
        "seconds": 2,
    }
    emit(
        AgentEventType.EXECUTION,
        block=completed,
        metrics={"llm_seconds": 2, "output_tokens": 10, "tokens_per_second": 5},
    )
    emit(AgentEventType.EXECUTION, metrics={"tool_seconds": 1})
    main = task["agents"]["main"]
    assert main["blocks"] == [completed, tool]
    assert main["context_tokens"] == 12
    assert main["tool_seconds"] == 1
    assert main["llm_seconds"] == 2
    assert main["output_tokens"] == 10
    assert main["tokens_per_second"] == 5

    def reviewer(event, **payload):
        emit(
            AgentEventType.REVIEWER,
            reviewer_id="coverage",
            role=AgentRole.NOTE_REVIEW,
            event=event,
            round=1,
            **payload,
        )

    reviewer(ReviewerEventType.STARTED)
    reviewer(
        AgentEventType.EXECUTION, block=completed, metrics={"model": "review-model"}
    )
    report = {
        "correct": True,
        "explanation": "Covered",
        "findings": [],
    }
    reviewer(ReviewerEventType.FINISHED, content=json.dumps(report))
    child = task["agents"]["coverage"]
    assert child["review_status"] == ReviewStatus.PASSED
    assert child["model"] == "review-model"
    assert child["blocks"][0] == completed
    assert child["blocks"][1]["kind"] == ExecutionBlockKind.REVIEW
    assert child["blocks"][1]["report"] == report
    emit(AgentEventType.FILE, file="note_knowledge.md")
    assert child["review_status"] == ReviewStatus.NOT_REVIEWED
    blocks = note_log_blocks({"session_id": "lecture", "tasks": [task], "error": ""})
    assert blocks == [
        {
            "type": BlockType.AGENT_ACTIVITY,
            "send_action": "notes-pipeline.message",
            "send_values": {"session_id": "lecture"},
            "id": "lecture",
            "agents": list(task["agents"].values()),
        }
    ]
    assert all(
        "history" not in card and "activity" not in card for card in blocks[0]["agents"]
    )
    reviewer(ReviewerEventType.FAILED, error="Review failed")
    assert child["status"] == ExecutionStatus.FAILED
    assert child["blocks"][1]["status"] == ExecutionBlockStatus.FAILED
    assert child["blocks"][1]["error"] == "Review failed"


@pytest.mark.parametrize("correct", [True, False])
def test_review_replaces_only_final_output(correct):
    import json
    from core.events import Event

    pipeline = NotesPipelinePlugin()
    pipeline.condition = threading.Condition()
    pipeline.ctx = SimpleNamespace(ui=SimpleNamespace(refresh=lambda reason: None))
    task = {"agents": {}, "label": "Writer"}
    pipeline.agent_tasks = {"writer": task}

    def emit(event, round, **payload):
        pipeline._agent_event(
            Event(
                AgentEventType.REVIEWER,
                {
                    "session_id": "writer",
                    "reviewer_id": "reviewer",
                    "role": AgentRole.NOTE_REVIEW,
                    "event": event,
                    "round": round,
                    **payload,
                },
                0,
                0,
            )
        )

    report = {"correct": correct, "explanation": "Covered", "findings": []}
    content = json.dumps(report)
    for round in (1, 2):
        emit(ReviewerEventType.STARTED, round)
        child = task["agents"]["reviewer"]
        assert child["status"] == ExecutionStatus.RUNNING
        for suffix in ("commentary", "output-a", "output-b"):
            emit(
                AgentEventType.EXECUTION,
                round,
                block={
                    "id": f"{round}:{suffix}",
                    "kind": ExecutionBlockKind.TEXT,
                    "text": content,
                    "status": ExecutionBlockStatus.COMPLETED,
                },
            )
        emit(
            ReviewerEventType.FINISHED,
            round,
            content=content,
            block_ids=[f"{round}:output-a", f"{round}:output-b"],
        )
        assert child["review_status"] == (
            ReviewStatus.PASSED if correct else ReviewStatus.FAILED
        )

    assert [block["id"] for block in child["blocks"]] == [
        "1:commentary",
        "reviewer:review:1",
        "2:commentary",
        "reviewer:review:2",
    ]
    assert [block["report"] for block in child["blocks"] if "report" in block] == [
        report,
        report,
    ]
    pipeline._agent_event(Event(AgentEventType.FILE, {"session_id": "writer"}, 0, 0))
    assert child["review_status"] == ReviewStatus.NOT_REVIEWED
    assert child["blocks"][1]["report"] == report

    emit(
        ReviewerEventType.FAILED,
        3,
        content='{"overall_correctness":"correct"}',
        error="Invalid review report",
        block_ids=[],
    )
    assert len(child["blocks"]) == 5
    assert child["blocks"][-1]["id"] == "reviewer:review:3"
    assert child["blocks"][-1]["status"] == ExecutionBlockStatus.FAILED
    assert "report" not in child["blocks"][-1]
    assert child["review_status"] == ReviewStatus.FAILED


@pytest.mark.parametrize("correct", ["true", 1, None])
def test_review_rejects_non_boolean(correct):
    import json
    from core.events import Event

    pipeline = NotesPipelinePlugin()
    pipeline.condition = threading.Condition()
    pipeline.ctx = SimpleNamespace(ui=SimpleNamespace(refresh=lambda reason: None))
    task = {"agents": {}, "label": "Writer"}
    pipeline.agent_tasks = {"writer": task}
    pipeline._agent_event(
        Event(
            AgentEventType.REVIEWER,
            {
                "session_id": "writer",
                "reviewer_id": "reviewer",
                "event": ReviewerEventType.FINISHED,
                "round": 1,
                "content": json.dumps({"correct": correct}),
            },
            0,
            0,
        )
    )
    child = task["agents"]["reviewer"]
    assert child["review_status"] == ReviewStatus.FAILED
    assert child["blocks"][0]["kind"] == ExecutionBlockKind.REVIEW
    assert child["blocks"][0]["status"] == ExecutionBlockStatus.FAILED
    assert "report" not in child["blocks"][0]


def test_concurrent_chunks_share_one_viewer():
    from plugins.summarize.plugin import note_log_blocks

    tasks = [
        {
            "agents": {
                f"chunk-{index}": {
                    "role": ExecutionRole.CHUNK_WRITER,
                    "label": f"片段 {index}",
                }
            }
        }
        for index in range(3)
    ]
    blocks = note_log_blocks({"session_id": "lecture", "tasks": tasks, "error": ""})
    assert len(blocks) == 1
    assert [agent["role"] for agent in blocks[0]["agents"]] == [
        ExecutionRole.CHUNK_WRITER
    ] * 3


class ScriptedNoteAgent:
    def __init__(self, run):
        self.script = run
        self.sessions = {}
        self.calls = []
        self.follow_ups = []
        self.errors = []
        self.lock = threading.Lock()
        self.active = 0
        self.peak_active = 0
        self.next_id = 0

    def create_session(self, name, **kwargs):
        with self.lock:
            self.next_id += 1
            session = SimpleNamespace(id=str(self.next_id), name=name, **kwargs)
            self.sessions[session.id] = session
            return session

    def reset(self, sid):
        assert sid in self.sessions

    def run(self, sid, instruction, *, cancel_event=None, follow_ups=()):
        with self.lock:
            session = self.sessions[sid]
            self.calls.append((session, instruction))
            self.follow_ups.append(list(follow_ups))
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
        try:
            return self.script(session, instruction)
        except BaseException as exc:
            with self.lock:
                self.errors.append(exc)
            raise
        finally:
            with self.lock:
                self.active -= 1

    def close(self, sid):
        with self.lock:
            del self.sessions[sid]

    def can_resume(self, session_id):
        return True, ""


def note_result(message="done", error=None, review_status=ReviewStatus.NOT_REVIEWED):
    return SimpleNamespace(
        error=error, final_message=message, review_status=review_status
    )


def write_notes(session, body):
    for name in NOTE_HEADERS:
        session.workspace.write(name, f"# Notes\n\n{body}\n")
    return note_result()


@contextmanager
def note_pipeline(tmp_path, run, *, settings=None, releases=()):
    callbacks = {}
    updates = queue.Queue()

    def emit(name, **payload):
        for callback in tuple(callbacks.get(name, ())):
            callback(payload)

    def on(name, callback, **kwargs):
        callbacks.setdefault(name, []).append(callback)

    agent = ScriptedNoteAgent(run)
    hub = StreamHub(emit=emit)
    values = {
        "notes.chunk_seconds": 10,
        "notes.overlap_seconds": 2,
        "notes.concurrency": 3,
        "summarize.review_enabled": False,
    }
    values.update(settings or {})
    config = FakeSettings(values)
    ctx = SimpleNamespace(
        data_dir=tmp_path,
        streams=hub,
        require=lambda name: config if name == "settings" else agent,
        service=lambda name: agent if name == "stt" else None,
        register_service=lambda *args: None,
        events=SimpleNamespace(on=on),
        ui=SimpleNamespace(
            register_task_provider=lambda *args, **kwargs: None,
            register_action=lambda *args, **kwargs: None,
            unregister_action=lambda *args, **kwargs: None,
            refresh=updates.put,
        ),
    )
    pipeline = NotesPipelinePlugin()
    pipeline._agent_run = lambda sid, agent_id, instruction, **kwargs: agent.run(
        agent_id, instruction, follow_ups=kwargs.get("follow_ups", ())
    )
    pipeline.setup(ctx)

    def wait_until(predicate):
        deadline = time.monotonic() + 8
        while not predicate():
            remaining = deadline - time.monotonic()
            assert remaining > 0, (pipeline.running_tasks(), agent.errors)
            try:
                updates.get(timeout=remaining)
            except queue.Empty:
                raise AssertionError((pipeline.running_tasks(), agent.errors)) from None

    try:
        yield SimpleNamespace(
            pipeline=pipeline,
            hub=hub,
            agent=agent,
            updates=updates,
            wait_until=wait_until,
        )
    finally:
        for release in releases:
            release.set()
        pipeline.teardown()
    assert not agent.errors
    assert not agent.sessions


def test_overlap_does_not_duplicate_core():
    records = [
        {
            "start": 550,
            "end": 610,
            "text": "long proof",
            "stream": StreamKind.TRANSCRIPT,
        },
        {
            "start": 600,
            "end": 620,
            "text": "continuation",
            "stream": StreamKind.TRANSCRIPT,
        },
        {
            "start": 300,
            "end": 320,
            "text": "board premise",
            "stream": StreamKind.VISUAL,
        },
    ]
    first, _ = chunk_material(records, 0, 600, 60)
    second, context = chunk_material(records, 600, 1200, 60)
    assert [r["text"] for r in first] == ["long proof", "board premise"]
    assert [r["text"] for r in second] == ["continuation"]
    assert {r["text"] for r in context} == {"long proof", "board premise"}


def test_log_and_stream_record_once(tmp_path):
    import json
    from types import SimpleNamespace
    from core.streams import StreamHub
    from plugins.notes_pipeline.material import collect_material

    hub = StreamHub()
    session = hub.start_session()
    session.transcript.append("condition", start=1.0001, end=2.0001)
    directory = tmp_path / "sessions" / session.session_id
    directory.mkdir(parents=True)
    (directory / "transcript.jsonl").write_text(
        json.dumps({"start": 1.0, "end": 2.0, "text": "condition"}) + "\n"
    )
    ctx = SimpleNamespace(data_dir=tmp_path, streams=hub)
    assert [r["text"] for r in collect_material(ctx, session.session_id)] == [
        "condition"
    ]


def test_late_visual_window_blocks_chunk():
    from types import SimpleNamespace
    from core.streams import StreamHub
    from plugins.notes_pipeline.plugin import NotesPipelinePlugin

    hub = StreamHub()
    session = hub.start_session()
    pipeline = NotesPipelinePlugin()
    pipeline.ctx = SimpleNamespace(streams=hub, service=lambda name: object())
    hub.seal_text(session.session_id, StreamKind.TRANSCRIPT, 600)
    hub.seal_text(session.session_id, StreamKind.VISUAL, 580)
    before, final = pipeline._readiness(session.session_id, [])
    assert before == 580 and not final
    session.visual.append("late tail", start=580, end=620)
    hub.seal_text(session.session_id, StreamKind.VISUAL, 600)
    before, final = pipeline._readiness(session.session_id, [])
    assert before == 600 and not final
    core, _ = chunk_material(
        [{"start": 580, "end": 620, "text": "late tail", "stream": StreamKind.VISUAL}],
        0,
        600,
        60,
    )
    assert [record["text"] for record in core] == ["late tail"]


def test_disabled_source_does_not_end_lecture():
    from types import SimpleNamespace
    from core.streams import StreamHub
    from plugins.notes_pipeline.plugin import NotesPipelinePlugin

    hub = StreamHub()
    session = hub.start_session()
    pipeline = NotesPipelinePlugin()
    pipeline.ctx = SimpleNamespace(streams=hub, service=lambda name: object())
    hub.set_text_enabled(session.session_id, StreamKind.VISUAL, False)
    hub.seal_text(session.session_id, StreamKind.TRANSCRIPT, 600, final=True)
    assert not pipeline._readiness(session.session_id, [])[1]
    session.input_done = True
    assert pipeline._readiness(session.session_id, [])[1]


def test_chunks_publish_in_source_order(tmp_path):
    first_started = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()

    def run(session, instruction):
        source = session.workspace.text("source.md")
        if "first-core" in source:
            first_started.set()
            assert release_first.wait(8)
            return write_notes(session, "first-core")
        assert "second-core" in source
        result = write_notes(session, "second-core")
        second_finished.set()
        return result

    with note_pipeline(tmp_path, run, releases=(release_first,)) as env:
        session = env.hub.start_session()
        session.transcript.append("first-core", start=0, end=10)
        session.transcript.append("second-core", start=10, end=20)
        env.hub.seal_text(session.session_id, StreamKind.TRANSCRIPT, 20)
        assert first_started.wait(8)
        assert second_finished.wait(8)
        env.wait_until(
            lambda: env.pipeline.status(session.session_id)["running_chunks"] == 1
        )
        assert env.pipeline.status(session.session_id)["published_end"] == 0
        for path in env.pipeline.draft_paths(session.session_id).values():
            assert "second-core" not in path.read_text()
        release_first.set()
        env.wait_until(
            lambda: env.pipeline.status(session.session_id)["published_end"] == 20
        )
        for path in env.pipeline.draft_paths(session.session_id).values():
            text = path.read_text()
            assert text.count("first-core") == text.count("second-core") == 1
            assert text.index("first-core") < text.index("second-core")


def test_final_request_survives_busy_chunk(tmp_path):
    chunk_started = threading.Event()
    release_chunk = threading.Event()
    writer_started = threading.Event()
    sources = []

    def run(session, instruction):
        if "source.md" not in session.workspace:
            sources.append(session.workspace.text(SOURCE_FILENAME))
            writer_started.set()
            return write_notes(session, "Complete lecture and short tail")
        source = session.workspace.text("source.md")
        if "initial-core" in source:
            chunk_started.set()
            assert release_chunk.wait(8)
        return write_notes(session, source)

    with note_pipeline(tmp_path, run, releases=(release_chunk,)) as env:
        session = env.hub.start_session(meta={"total_media_seconds": 13})
        session.transcript.append("initial-core", start=0, end=10)
        env.hub.seal_text(session.session_id, StreamKind.TRANSCRIPT, 10)
        assert chunk_started.wait(8)
        status = env.pipeline.status(session.session_id)
        assert status["total_chunks"] == 1
        assert status["expected_chunks"] == 2
        assert env.pipeline.finalize(session.session_id)
        assert not env.pipeline.start(session.session_id)
        session.transcript.append("short-tail", start=10, end=13)
        env.hub.end_session()
        env.hub.seal_text(session.session_id, StreamKind.TRANSCRIPT, 13, final=True)
        assert not writer_started.is_set()
        release_chunk.set()
        env.wait_until(
            lambda: (
                env.pipeline.status(session.session_id)["phase"] == NotePhase.PUBLISHED
            )
        )
        assert len(sources) == 1
        assert "initial-core" in sources[0] and "short-tail" in sources[0]
        assert env.pipeline.status(session.session_id)["published_end"] == 13
        status = env.pipeline.status(session.session_id)
        assert (
            status["completed_chunks"]
            == status["total_chunks"]
            == status["expected_chunks"]
            == 2
        )
        assert "short-tail" in env.pipeline.source_path(session.session_id).read_text()


def test_failed_chunk_keeps_full_final_source(tmp_path):
    writer_started = threading.Event()
    release_writer = threading.Event()
    writer_inputs = {}

    def run(session, instruction):
        if "source.md" in session.workspace:
            write_notes(session, "Rejected partial draft")
            return note_result(error="Chunk generation failed")
        writer_inputs.update(
            (name, session.workspace.text(name))
            for name in session.workspace.names
            if name not in NOTE_HEADERS
        )
        writer_started.set()
        assert release_writer.wait(8)
        return write_notes(session, "Recovered from complete original source")

    with note_pipeline(tmp_path, run, releases=(release_writer,)) as env:
        session = env.hub.start_session()
        session.transcript.append(
            "Required theorem and every condition", start=0, end=10
        )
        env.hub.end_session()
        env.hub.seal_text(session.session_id, StreamKind.TRANSCRIPT, 10, final=True)
        assert env.pipeline.finalize(session.session_id)
        assert writer_started.wait(8)
        status = env.pipeline.status(session.session_id)
        assert status["published_end"] == 0
        assert status["errors"]
        assert "Required theorem and every condition" in writer_inputs[SOURCE_FILENAME]
        assert "failed" in writer_inputs["index.md"]
        assert not any(name.startswith("chunk_") for name in writer_inputs)
        assert all(
            "Rejected partial draft" not in text for text in writer_inputs.values()
        )
        assert env.pipeline.source_path(session.session_id) is None
        release_writer.set()
        env.wait_until(
            lambda: (
                env.pipeline.status(session.session_id)["phase"] == NotePhase.PUBLISHED
            )
        )
        assert env.pipeline.status(session.session_id)["errors"]
        assert "Required theorem and every condition" in (
            env.pipeline.source_path(session.session_id).read_text()
        )


def test_final_tasks_keep_session_sources_when_selection_changes(tmp_path):
    both_writers = threading.Barrier(3)
    release_writers = threading.Event()
    writer_sources = {}

    def run(session, instruction):
        sid = session.meta["lecture_session"]
        source = session.workspace.text(SOURCE_FILENAME)
        writer_sources[sid] = source
        both_writers.wait(8)
        assert release_writers.wait(8)
        return write_notes(session, source)

    with note_pipeline(
        tmp_path,
        run,
        settings={"notes.concurrency": 2, "instant-summarize.enabled": False},
        releases=(release_writers,),
    ) as env:
        lectures = []
        for marker in (
            "First lecture unique condition",
            "Second lecture unique theorem",
        ):
            session = env.hub.start_session(meta={"title": marker})
            session.transcript.append(marker, start=0, end=10)
            env.hub.end_session()
            env.hub.seal_text(session.session_id, StreamKind.TRANSCRIPT, 10, final=True)
            lectures.append((session.session_id, marker))
            assert env.pipeline.start(session.session_id)
        both_writers.wait(8)
        third = env.hub.start_session(meta={"title": "Unrelated selected lecture"})
        third.transcript.append("Do not copy this third lecture", start=0, end=1)
        env.hub.set_notes_session(third.session_id)
        release_writers.set()
        env.wait_until(
            lambda: all(
                env.pipeline.status(sid)["phase"] == NotePhase.PUBLISHED
                for sid, _ in lectures
            )
        )
        for sid, marker in lectures:
            other_marker = next(
                text for other_sid, text in lectures if other_sid != sid
            )
            assert marker in writer_sources[sid]
            assert other_marker not in writer_sources[sid]
            assert "Do not copy" not in writer_sources[sid]
            source = env.pipeline.source_path(sid).read_text()
            assert marker in source and other_marker not in source
            for filename, _ in env.pipeline.note_files(NotePublication.FINAL).values():
                text = (tmp_path / "sessions" / sid / "notes" / filename).read_text()
                assert marker in text
                assert other_marker not in text and "Do not copy" not in text
        assert env.pipeline.source_path(third.session_id) is None
        assert env.agent.peak_active == 2


def test_published_review_survives_restart(tmp_path):
    with note_pipeline(
        tmp_path, lambda session, instruction: write_notes(session, "Complete lecture")
    ) as env:
        session = env.hub.start_session()
        session.transcript.append("Definition", start=0, end=10)
        env.hub.end_session()
        env.hub.seal_text(session.session_id, StreamKind.TRANSCRIPT, 10, final=True)
        assert env.pipeline.finalize(session.session_id)
        env.wait_until(
            lambda: (
                env.pipeline.status(session.session_id)["phase"] == NotePhase.PUBLISHED
            )
        )
        expected = env.pipeline.status(session.session_id)["review_status"]
    with note_pipeline(
        tmp_path,
        lambda session, instruction: write_notes(session, "Unexpected regeneration"),
    ) as restored:
        assert restored.pipeline.status(session.session_id)["review_status"] == expected
        assert (
            restored.pipeline.status(session.session_id)["phase"] == NotePhase.PUBLISHED
        )


def test_manual_entries_keep_incremental_occurrences(tmp_path):
    sources = []

    def run(session, instruction):
        source = session.workspace.text("source.md")
        sources.append(source)
        return write_notes(session, source)

    with note_pipeline(tmp_path, run) as env:
        session = env.hub.start_session()
        session.transcript.append("Repeated condition")
        env.wait_until(lambda: len(sources) == 1)
        session.transcript.append("Repeated condition")
        env.wait_until(lambda: len(sources) == 2)
        assert all(source.count("Repeated condition") == 1 for source in sources)
        from plugins.notes_pipeline.material import collect_material

        assert len(collect_material(env.pipeline.ctx, session.session_id)) == 2


def test_lecture_context_uses_target_session(tmp_path):
    hub = StreamHub()
    target = hub.start_session(
        meta={"course_name": "Target course", "date": "2026-11-10"}
    )
    hub.start_session(meta={"course_name": "Other course"})
    pipeline = NotesPipelinePlugin()
    pipeline.ctx = SimpleNamespace(streams=hub, data_dir=tmp_path)
    context = pipeline._lecture_context(target.session_id)
    assert "Target course" in context and "2026-11-10" in context
    assert "Other course" not in context


def test_preview_omits_chunk_titles(tmp_path):
    pipeline = NotesPipelinePlugin()
    pipeline.ctx = SimpleNamespace(
        data_dir=tmp_path, ui=SimpleNamespace(refresh=lambda _: None)
    )
    pipeline.closed = False
    pipeline.settings = FakeSettings()
    pipeline.sessions = {}
    sid = "lecture"
    state = pipeline._state(sid)
    state["chunks"] = [
        {"index": index, "end": (index + 1) * 10, "state": ChunkState.DONE}
        for index in range(3)
    ]
    for index in range(3):
        directory = pipeline._directory(sid) / "chunks" / str(index)
        directory.mkdir(parents=True)
        for name, header in NOTE_HEADERS.items():
            body = (
                "## 作业\n\n完成练习 1．\n"
                if index == 1 and name == "note_notice.md"
                else ""
            )
            (directory / name).write_text(header + body)
    pipeline._publish_preview(sid)
    text = (
        tmp_path / "sessions" / sid / "notes" / "instant_note_notice.md"
    ).read_text()
    assert "笔记：通知" not in text
    assert text.count("# 通知\n") == 1
    assert "## 作业\n\n完成练习 1．" in text


def saved_writer(pipeline, sid="lecture"):
    import json

    root = pipeline._directory(sid) / "final" / "frozen"
    root.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in (*NOTE_HEADERS, SOURCE_FILENAME, "index.md"):
        path = root / name
        path.write_text(NOTE_HEADERS.get(name, "Frozen source") + "\nSaved body\n")
        files[name] = str(path.relative_to(pipeline._directory(sid)))
    notes = pipeline.ctx.data_dir / "sessions" / sid / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    for name in NOTE_HEADERS:
        (notes / name).write_text("# Published\n\nLatest body\n")
    runtime = pipeline.ctx.data_dir / "llm_runtime"
    runtime.mkdir(exist_ok=True)
    transcript = runtime / "writer.jsonl"
    transcript.write_text(json.dumps({"type": "session", "id": "writer"}) + "\n")
    (runtime / "session-writer.path").write_text(str(transcript))
    state = pipeline._state(sid)
    state.update(
        phase=NotePhase.PUBLISHED,
        writer={
            "session_id": "writer",
            "name": "Saved writer",
            "role": AgentRole.NOTE_WRITER,
            "system_prompt": "Saved system",
            "tools": [AgentToolName.READ, AgentToolName.WRITE],
            "directory": "final/frozen",
            "files": files,
            "readonly": [SOURCE_FILENAME, "index.md"],
            "review": {"prompt": "Review", "system": "Reviewer", "maxRounds": 3},
        },
    )
    pipeline._save(state)
    return state


def test_continuation_admission_stages(tmp_path):
    from core.events import Event

    with note_pipeline(tmp_path, lambda session, instruction: note_result()) as env:
        pipeline = env.pipeline
        with pipeline.condition:
            state = saved_writer(pipeline)
            payload = {
                "session_id": "lecture",
                "agent_session_id": "writer",
                "message": "first\nline",
            }
            assert pipeline.message(payload) == {"ok": True, "refresh": True}
            assert pipeline.message({**payload, "message": "second"})["ok"]
            assert list(pipeline.queue) == [("lecture", NoteTaskKind.FINAL, -1)]
            assert pipeline.writer_messages["lecture"] == ["first\nline", "second"]
            assert (
                pipeline.status("lecture")["generation_state"]
                == GenerationState.GENERATING
            )
            pipeline.queue.clear()
            pipeline.writer_messages.pop("lecture")
            pipeline.continuations["lecture"] = ContinuationStage.STARTING
            assert not pipeline.message(payload)["ok"]
            task = {
                "session_id": "lecture",
                "agents": {
                    "main": pipeline._execution_agent(
                        "main", ExecutionRole.WRITER, "Writer", session_id="writer"
                    ),
                    "review": pipeline._execution_agent(
                        "review", ExecutionRole.REVIEWER, "Review", session_id="child"
                    ),
                },
            }
            pipeline.agent_tasks["writer"] = task
            task["ready"] = True
            pipeline._agent_event(
                Event(AgentEventType.RUN_READY, {"session_id": "writer"}, 0, 0)
            )
            assert pipeline.continuations["lecture"] == ContinuationStage.LIVE
            sent = []
            env.agent.message = lambda target, text: (
                sent.append((target, text)) or {"accepted": True}
            )
            assert pipeline.message(payload) == {
                "ok": True,
                "message": "",
                "refresh": False,
            }
            assert pipeline.message({**payload, "agent_session_id": "child"})["ok"]
            assert sent == [("writer", "first\nline"), ("child", "first\nline")]
            assert not pipeline.message({**payload, "session_id": "other"})["ok"]
            pipeline.continuations["lecture"] = ContinuationStage.FINISHING
            assert not pipeline.message(payload)["ok"]
            pipeline.agent_tasks.clear()
            pipeline.continuations.clear()
            state["phase"] = NotePhase.PUBLISHED
            assert not pipeline.message({**payload, "agent_session_id": "child"})["ok"]
            assert not pipeline.message(
                {**payload, "agent_session_id": "completed-chunk"}
            )["ok"]
            assert pipeline.message(payload)["ok"]
            pipeline.queue.clear()
            pipeline.writer_messages.clear()
            pipeline.continuations.clear()


@pytest.mark.parametrize(
    "review_enabled, verdict, expected_status",
    [
        (True, ReviewStatus.NOT_REVIEWED, ReviewStatus.NOT_REVIEWED),
        (True, ReviewStatus.PASSED, ReviewStatus.PASSED),
        (True, ReviewStatus.FAILED, ReviewStatus.FAILED),
        (False, ReviewStatus.NOT_REVIEWED, ReviewStatus.PASSED),
    ],
)
def test_continuation_reopens_saved_writer_and_preserves_qa(
    tmp_path, review_enabled, verdict, expected_status
):
    from core.note_document import build_block, parse_blocks

    def run(session, instruction):
        write_notes(session, "Revised body")
        return note_result(review_status=verdict)

    with note_pipeline(tmp_path, run) as env:
        pipeline = env.pipeline
        with pipeline.condition:
            state = saved_writer(pipeline)
            if not review_enabled:
                state["writer"]["review"] = None
            notes = tmp_path / "sessions" / "lecture" / "notes"
            qa = build_block(
                "qa-saved", "2026-11-17", question="Question", answer="Answer"
            )
            path = notes / "note_knowledge.md"
            path.write_text(path.read_text() + "\n" + qa)
            frozen_source = (
                pipeline._directory("lecture")
                / state["writer"]["files"][SOURCE_FILENAME]
            ).read_bytes()
            reopened = []

            def reopen(session_id, **params):
                assert session_id == "writer"
                assert "Latest body" in params["workspace"].text("note_knowledge.md")
                assert "qa:begin" not in params["workspace"].text("note_knowledge.md")
                if review_enabled:
                    assert params["meta"]["review"] == state["writer"]["review"]
                else:
                    assert "review" not in params["meta"]
                session = SimpleNamespace(id=session_id, **params)
                env.agent.sessions[session_id] = session
                reopened.append(session)
                return session

            env.agent.reopen = reopen
            pipeline.active["continuation"] = {"session_id": "lecture"}
            pipeline.continuations["lecture"] = ContinuationStage.STARTING
        pipeline._execute(
            "continuation",
            "lecture",
            NoteTaskKind.FINAL,
            -1,
            [],
            ["first\nline", "second"],
        )
        assert pipeline.status("lecture")["phase"] == NotePhase.PUBLISHED
        assert (
            pipeline.status("lecture")["generation_state"] == GenerationState.FINISHED
        )
        assert not pipeline.continuations and not pipeline.writer_messages
        assert len(reopened) == len(env.agent.calls) == 1
        assert env.agent.calls[0][1] == "first\nline"
        assert env.agent.follow_ups == [["second"]]
        assert pipeline.status("lecture")["review_status"] == expected_status
        published = path.read_text()
        assert "Revised body" in published
        preserved = parse_blocks(published)
        expected = parse_blocks(qa)
        assert [
            (block.id, block.question, block.answer, block.status)
            for block in preserved
        ] == [
            (block.id, block.question, block.answer, block.status) for block in expected
        ]
        assert published.index("Revised body") < preserved[0].start
        assert (
            pipeline._directory("lecture") / state["writer"]["files"][SOURCE_FILENAME]
        ).read_bytes() == frozen_source
        assert not env.agent.sessions


def test_failed_continuation_can_retry(tmp_path):
    def run(session, instruction):
        if instruction == "First revision":
            write_notes(session, "Unpublished revision")
            return note_result(error="Continuation generation failed")
        assert instruction == "Retry revision"
        assert "Latest body" in session.workspace.text("note_knowledge.md")
        return write_notes(session, "Recovered revision")

    with note_pipeline(tmp_path, run) as env:
        pipeline = env.pipeline

        def reopen(session_id, **params):
            session = SimpleNamespace(id=session_id, **params)
            env.agent.sessions[session_id] = session
            return session

        env.agent.reopen = reopen
        with pipeline.condition:
            state = saved_writer(pipeline)
            notes = tmp_path / "sessions" / "lecture" / "notes"
            published = {name: (notes / name).read_bytes() for name in NOTE_HEADERS}
            payload = {
                "session_id": "lecture",
                "agent_session_id": "writer",
                "message": "First revision",
            }
            assert pipeline.message(payload)["ok"]
            assert pipeline.condition.wait_for(
                lambda: state["phase"] == NotePhase.FAILED and not pipeline.active,
                timeout=8,
            )
            status = pipeline.status("lecture")
            assert status["error"] == "Continuation generation failed"
            assert status["generation_state"] == GenerationState.FINISHED
            assert not state["finalize_requested"]
            assert not pipeline.continuations and not pipeline.writer_messages
            assert {
                name: (notes / name).read_bytes() for name in NOTE_HEADERS
            } == published
            assert pipeline.message({**payload, "message": "Retry revision"})["ok"]
            assert pipeline.condition.wait_for(
                lambda: state["phase"] == NotePhase.PUBLISHED and not pipeline.active,
                timeout=8,
            )
            assert any(
                row["error"] == "Continuation generation failed"
                for row in pipeline.status("lecture")["errors"]
            )
        assert [instruction for _, instruction in env.agent.calls] == [
            "First revision",
            "Retry revision",
        ]
        assert all(
            "Recovered revision" in (notes / name).read_text() for name in NOTE_HEADERS
        )
        assert not env.agent.sessions


@pytest.mark.parametrize("draft", [False, True])
def test_initial_failure_cannot_continue(tmp_path, draft):
    def run(session, instruction):
        write_notes(session, "Unpublished initial notes")
        if draft:
            env.pipeline._publish_lecture(
                session.meta["lecture_session"], session.workspace, draft=True
            )
        return note_result(error="Initial generation failed")

    with note_pipeline(
        tmp_path, run, settings={"instant-summarize.enabled": False}
    ) as env:
        pipeline = env.pipeline
        session = env.hub.start_session()
        session.transcript.append("Definition", start=0, end=10)
        env.hub.end_session()
        env.hub.seal_text(session.session_id, StreamKind.TRANSCRIPT, 10, final=True)
        with pipeline.condition:
            assert pipeline.finalize(session.session_id)
            state = pipeline._state(session.session_id)
            assert pipeline.condition.wait_for(
                lambda: state["phase"] == NotePhase.FAILED and not pipeline.active,
                timeout=8,
            )
            writer_id = state["writer"]["session_id"]
            runtime = tmp_path / "llm_runtime"
            runtime.mkdir(exist_ok=True)
            transcript = runtime / f"{writer_id}.jsonl"
            transcript.write_text("{}\n")
            (runtime / f"session-{writer_id}.path").write_text(str(transcript))
            assert state["finalize_requested"]
            assert not pipeline.message(
                {
                    "session_id": session.session_id,
                    "agent_session_id": writer_id,
                    "message": "Continue",
                }
            )["ok"]
            assert (
                pipeline.status(session.session_id)["error"]
                == "Initial generation failed"
            )
            assert not pipeline.queue and not pipeline.continuations
        notes = tmp_path / "sessions" / session.session_id / "notes"
        assert all((notes / name).exists() == draft for name in NOTE_HEADERS)
        assert len(env.agent.calls) == 1


def test_continuation_requires_saved_transcript(tmp_path):
    with note_pipeline(tmp_path, lambda session, instruction: note_result()) as env:
        pipeline = env.pipeline
        with pipeline.condition:
            saved_writer(pipeline)
            env.agent.can_resume = lambda session_id: (False, "transcript unavailable")
            result = pipeline.message(
                {
                    "session_id": "lecture",
                    "agent_session_id": "writer",
                    "message": "Continue",
                }
            )
            assert not result["ok"]
            assert "会话记录不可用" in result["message"]
            assert not pipeline.queue and not env.agent.calls
            assert pipeline.status("lecture")["phase"] == NotePhase.PUBLISHED


def test_stop_discards_queued_continuation(tmp_path):
    with note_pipeline(tmp_path, lambda session, instruction: note_result()) as env:
        pipeline = env.pipeline
        with pipeline.condition:
            saved_writer(pipeline)
            payload = {
                "session_id": "lecture",
                "agent_session_id": "writer",
                "message": "Continue",
            }
            assert pipeline.message(payload)["ok"]
            assert pipeline.stop("lecture")
            assert not pipeline.queue and not pipeline.writer_messages
            assert not pipeline.continuations
            assert not pipeline.message(payload)["ok"]
            assert pipeline.status("lecture")["phase"] == NotePhase.CANCELLED


def test_restored_writer_is_selectable(tmp_path):
    with note_pipeline(tmp_path, lambda session, instruction: note_result()) as env:
        with env.pipeline.condition:
            saved_writer(env.pipeline)
    with note_pipeline(tmp_path, lambda session, instruction: note_result()) as env:
        state = env.pipeline.status("lecture")
        assert state["phase"] == NotePhase.PUBLISHED
        assert state["tasks"][0]["agents"]["main"]["session_id"] == "writer"
        assert (
            state["tasks"][0]["agents"]["main"]["status"] == ExecutionStatus.COMPLETED
        )


def test_coordinator_claims_one_continuation_batch(tmp_path):
    started = threading.Event()
    release = threading.Event()
    claimed = []
    with note_pipeline(
        tmp_path, lambda session, instruction: note_result(), releases=(release,)
    ) as env:
        pipeline = env.pipeline

        def execute(token, sid, kind, index, records, messages=None):
            claimed.append((sid, kind, messages))
            started.set()
            assert release.wait(8)
            with pipeline.condition:
                pipeline.active.pop(token)
                pipeline.continuations.pop(sid)
                state = pipeline._state(sid)
                state.update(phase=NotePhase.PUBLISHED, finalize_requested=False)
                pipeline.condition.notify_all()
            pipeline.ctx.ui.refresh("released")

        pipeline._execute = execute
        with pipeline.condition:
            saved_writer(pipeline)
            payload = {
                "session_id": "lecture",
                "agent_session_id": "writer",
                "message": "first",
            }
            assert pipeline.message(payload)["ok"]
            assert pipeline.message({**payload, "message": "second"})["ok"]
        assert started.wait(3)
        assert claimed == [("lecture", NoteTaskKind.FINAL, ["first", "second"])]
        assert not pipeline.message({**payload, "message": "while starting"})["ok"]
        release.set()
        env.wait_until(lambda: not pipeline.active)
        assert not pipeline.queue and not pipeline.writer_messages


def test_stale_review_does_not_show_passed(tmp_path):
    import json
    from core.events import Event

    with note_pipeline(tmp_path, lambda session, instruction: note_result()) as env:
        with env.pipeline.condition:
            saved_writer(env.pipeline)
            task = {"session_id": "lecture", "agents": {}, "label": "Writer"}
            env.pipeline.agent_tasks["writer"] = task
            content = json.dumps(
                {
                    "correct": True,
                    "findings": [],
                    "explanation": "Snapshot",
                }
            )
            env.pipeline._agent_event(
                Event(
                    AgentEventType.REVIEWER,
                    {
                        "session_id": "writer",
                        "reviewer_id": "review-child",
                        "role": AgentRole.NOTE_REVIEW,
                        "event": ReviewerEventType.FINISHED,
                        "round": 1,
                        "stale": True,
                        "content": content,
                    },
                    0,
                    0,
                )
            )
            child = task["agents"]["review-child"]
            assert child["session_id"] == "review-child"
            assert child["review_status"] == ReviewStatus.NOT_REVIEWED
            env.pipeline.agent_tasks.clear()


def test_continuation_accepts_sdk_title_envelope(tmp_path):
    import json

    with note_pipeline(tmp_path, lambda session, instruction: note_result()) as env:
        with env.pipeline.condition:
            saved_writer(env.pipeline)
            transcript = tmp_path / "llm_runtime" / "writer.jsonl"
            transcript.write_text(
                "\n".join(
                    json.dumps(entry)
                    for entry in (
                        {
                            "type": "title",
                            "v": 1,
                            "title": "",
                            "updatedAt": "2026-11-17T10:45:13.588Z",
                            "pad": " " * 174,
                        },
                        {
                            "type": "session",
                            "version": 3,
                            "id": "sdk-session",
                            "timestamp": "2026-11-17T10:45:13.588Z",
                            "cwd": str(tmp_path),
                        },
                        {
                            "type": "message",
                            "id": "user-message",
                            "parentId": None,
                            "message": {
                                "role": "user",
                                "content": [{"type": "text", "text": "Original task"}],
                            },
                        },
                    )
                )
                + "\n"
            )
            result = env.pipeline.message(
                {
                    "session_id": "lecture",
                    "agent_session_id": "writer",
                    "message": "Continue",
                }
            )
            assert result == {"ok": True, "refresh": True}
            assert env.pipeline.writer_messages["lecture"] == ["Continue"]
            env.pipeline.queue.clear()
            env.pipeline.writer_messages.clear()
            env.pipeline.continuations.clear()
