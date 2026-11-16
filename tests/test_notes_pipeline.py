"""Consumer contracts for note material boundaries and publication."""

from plugins.notes_pipeline.material import chunk_material

import queue
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

from core.streams import StreamHub
from plugins.notes_pipeline.plugin import NotesPipelinePlugin
from plugins.notes_pipeline.prompts import NOTE_HEADERS, SOURCE_FILENAME
from tests.support import FakeSettings


class ScriptedNoteAgent:
    def __init__(self, run):
        self.script = run
        self.sessions = {}
        self.calls = []
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

    def run(self, sid, instruction, *, cancel_event=None):
        with self.lock:
            session = self.sessions[sid]
            self.calls.append((session, instruction))
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


def note_result(message="done", error=None):
    return SimpleNamespace(error=error, final_message=message)


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
        "instant-summarize.retry_max": 0,
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
            refresh=updates.put,
        ),
    )
    pipeline = NotesPipelinePlugin()
    pipeline._agent_run = lambda sid, agent_id, instruction: agent.run(
        agent_id, instruction
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
        {"start": 550, "end": 610, "text": "long proof", "stream": "transcript"},
        {"start": 600, "end": 620, "text": "continuation", "stream": "transcript"},
        {"start": 300, "end": 320, "text": "board premise", "stream": "visual"},
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


def test_reviewers_overlap_after_writer(tmp_path):
    import threading
    from types import SimpleNamespace
    from core.streams import StreamHub
    from plugins.notes_pipeline.plugin import NotesPipelinePlugin
    from plugins.notes_pipeline.prompts import NOTE_HEADERS
    from tests.support import FakeSettings

    barrier = threading.Barrier(2)
    published = threading.Event()
    writer_done = threading.Event()

    class Agent:
        def __init__(self):
            self.sessions = {}
            self.next_id = 0
            self.lock = threading.Lock()

        def create_session(self, name, **kwargs):
            with self.lock:
                self.next_id += 1
                session = SimpleNamespace(
                    id=str(self.next_id), name=name, workspace=kwargs["workspace"]
                )
                self.sessions[session.id] = session
                return session

        def run(self, sid, instruction, *, cancel_event=None):
            session = self.sessions[sid]
            if session.name.endswith(("coverage", "correctness")):
                assert writer_done.is_set()
                barrier.wait(3)
                return SimpleNamespace(error=None, final_message="REVIEW: PASS")
            for name in NOTE_HEADERS:
                session.workspace.write(name, "# Notes\n\nComplete source details.\n")
            writer_done.set()
            return SimpleNamespace(error=None, final_message="done")

        def close(self, sid):
            self.sessions.pop(sid)

    settings = FakeSettings(
        {"notes.concurrency": 3, "instant-summarize.enabled": False}
    )
    agent = Agent()
    hub = StreamHub()
    session = hub.start_session()
    session.transcript.append("A definition with conditions", start=0, end=10)
    hub.end_session()
    ctx = SimpleNamespace(
        data_dir=tmp_path,
        streams=hub,
        require=lambda name: settings if name == "settings" else agent,
        service=lambda name: None,
        register_service=lambda *args: None,
        events=SimpleNamespace(on=lambda *args, **kwargs: None),
        ui=SimpleNamespace(
            register_task_provider=lambda *args, **kwargs: None,
            refresh=lambda event: (
                published.set() if event == "notes.published" else None
            ),
        ),
    )
    pipeline = NotesPipelinePlugin()
    pipeline._agent_run = lambda sid, agent_id, instruction: agent.run(
        agent_id, instruction
    )
    pipeline.setup(ctx)
    try:
        assert pipeline.start(session.session_id)
        assert published.wait(5), pipeline.status(session.session_id)
        assert pipeline.status(session.session_id)["review_status"] == "passed"
        assert (
            (tmp_path / "sessions" / session.session_id / "notes" / "note_knowledge.md")
            .read_text()
            .endswith("Complete source details.\n")
        )
    finally:
        pipeline.teardown()


def test_late_visual_window_blocks_chunk():
    from types import SimpleNamespace
    from core.streams import StreamHub
    from plugins.notes_pipeline.plugin import NotesPipelinePlugin

    hub = StreamHub()
    session = hub.start_session()
    pipeline = NotesPipelinePlugin()
    pipeline.ctx = SimpleNamespace(streams=hub, service=lambda name: object())
    hub.seal_text(session.session_id, "transcript", 600)
    hub.seal_text(session.session_id, "visual", 580)
    before, final = pipeline._readiness(session.session_id, [])
    assert before == 580 and not final
    session.visual.append("late tail", start=580, end=620)
    hub.seal_text(session.session_id, "visual", 600)
    before, final = pipeline._readiness(session.session_id, [])
    assert before == 600 and not final
    core, _ = chunk_material(
        [{"start": 580, "end": 620, "text": "late tail", "stream": "visual"}],
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
    hub.set_text_enabled(session.session_id, "visual", False)
    hub.seal_text(session.session_id, "transcript", 600, final=True)
    assert not pipeline._readiness(session.session_id, [])[1]
    session.input_done = True
    assert pipeline._readiness(session.session_id, [])[1]


def test_chunks_publish_in_source_order(tmp_path):
    first_started = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()

    def run(session, instruction):
        source = session.workspace.read_lines("source.md", 1, -1)
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
        env.hub.seal_text(session.session_id, "transcript", 20)
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
        if SOURCE_FILENAME in session.workspace:
            sources.append(session.workspace.read_lines(SOURCE_FILENAME, 1, -1))
            writer_started.set()
            return write_notes(session, "Complete lecture and short tail")
        source = session.workspace.read_lines("source.md", 1, -1)
        if "initial-core" in source:
            chunk_started.set()
            assert release_chunk.wait(8)
        return write_notes(session, source)

    with note_pipeline(tmp_path, run, releases=(release_chunk,)) as env:
        session = env.hub.start_session()
        session.transcript.append("initial-core", start=0, end=10)
        env.hub.seal_text(session.session_id, "transcript", 10)
        assert chunk_started.wait(8)
        assert env.pipeline.start(session.session_id)
        assert env.pipeline.start(session.session_id)
        session.transcript.append("short-tail", start=10, end=13)
        env.hub.end_session()
        env.hub.seal_text(session.session_id, "transcript", 13, final=True)
        assert not writer_started.is_set()
        release_chunk.set()
        env.wait_until(
            lambda: env.pipeline.status(session.session_id)["phase"] == "published"
        )
        assert len(sources) == 1
        assert "initial-core" in sources[0] and "short-tail" in sources[0]
        assert env.pipeline.status(session.session_id)["published_end"] == 13
        assert "short-tail" in env.pipeline.source_path(session.session_id).read_text()


def test_failed_chunk_keeps_full_final_source(tmp_path):
    writer_started = threading.Event()
    release_writer = threading.Event()
    writer_inputs = {}

    def run(session, instruction):
        if SOURCE_FILENAME not in session.workspace:
            write_notes(session, "Rejected partial draft")
            return note_result(error="Chunk generation failed")
        writer_inputs.update(
            (name, session.workspace.read_lines(name, 1, -1))
            for name in session.workspace.visible_names
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
        env.hub.seal_text(session.session_id, "transcript", 10, final=True)
        assert env.pipeline.start(session.session_id)
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
            lambda: env.pipeline.status(session.session_id)["phase"] == "published"
        )
        assert env.pipeline.status(session.session_id)["errors"]
        assert "Required theorem and every condition" in (
            env.pipeline.source_path(session.session_id).read_text()
        )


def test_single_slot_completes_writer_and_both_reviews(tmp_path):
    roles = []

    def run(session, instruction):
        role = session.meta.get("role")
        roles.append(role)
        if role in ("coverage", "correctness"):
            return note_result("REVIEW: PASS")
        return write_notes(session, "Complete checked lecture")

    with note_pipeline(
        tmp_path,
        run,
        settings={"notes.concurrency": 1, "summarize.review_enabled": True},
    ) as env:
        session = env.hub.start_session()
        session.transcript.append("Original first core", start=0, end=10)
        session.transcript.append("Original short tail", start=10, end=13)
        env.hub.end_session()
        env.hub.seal_text(session.session_id, "transcript", 13, final=True)
        assert env.pipeline.start(session.session_id)
        env.wait_until(
            lambda: env.pipeline.status(session.session_id)["phase"] == "published"
        )
        assert roles.count("notes") == 2
        assert roles.count("writer") == 1
        assert roles.count("coverage") == roles.count("correctness") == 1
        assert roles.index("writer") > max(
            i for i, role in enumerate(roles) if role == "notes"
        )
        assert env.agent.peak_active == 1
        assert env.pipeline.status(session.session_id)["review_status"] == "passed"


def test_failed_review_edits_then_rechecks_both_items(tmp_path):
    writer_started = threading.Event()
    release_writer = threading.Event()
    editor_started = threading.Event()
    release_editor = threading.Event()
    review_barriers = [threading.Barrier(2), threading.Barrier(2)]
    review_started = [threading.Event(), threading.Event()]
    release_reviews = [threading.Event(), threading.Event()]
    counts = {"coverage": 0, "correctness": 0}
    reviewer_ids = {"coverage": [], "correctness": []}
    writer_ids = []

    def run(session, instruction):
        role = session.meta["role"]
        if role == "writer":
            writer_ids.append(session.id)
            if len(writer_ids) == 1:
                writer_started.set()
                assert release_writer.wait(8)
                return write_notes(session, "Draft missing a theorem condition")
            assert all(count == 1 for count in counts.values())
            assert "REVIEW: FAIL" in instruction
            assert "Restore the theorem condition" in instruction
            editor_started.set()
            assert release_editor.wait(8)
            return write_notes(session, "The theorem includes its required condition")
        assert role in counts
        round_index = counts[role]
        counts[role] += 1
        reviewer_ids[role].append(session.id)
        expected = (
            "Draft missing a theorem condition"
            if round_index == 0
            else "The theorem includes its required condition"
        )
        for name in NOTE_HEADERS:
            assert expected in session.workspace.read_lines(name, 1, -1)
        review_barriers[round_index].wait(8)
        review_started[round_index].set()
        assert release_reviews[round_index].wait(8)
        if round_index == 0 and role == "coverage":
            return note_result(
                "REVIEW: FAIL\nRestore the theorem condition at 0 seconds."
            )
        return note_result("REVIEW: PASS")

    with note_pipeline(
        tmp_path,
        run,
        settings={
            "instant-summarize.enabled": False,
            "summarize.review_enabled": True,
            "summarize.review_max_rounds": 3,
        },
        releases=(release_writer, release_editor, *release_reviews),
    ) as env:
        session = env.hub.start_session()
        session.transcript.append(
            "The theorem requires this condition", start=0, end=10
        )
        env.hub.end_session()
        env.hub.seal_text(session.session_id, "transcript", 10, final=True)
        assert env.pipeline.start(session.session_id)
        assert writer_started.wait(8)
        assert counts == {"coverage": 0, "correctness": 0}
        release_writer.set()
        assert review_started[0].wait(8)
        assert not editor_started.is_set()
        assert env.pipeline.source_path(session.session_id) is None
        release_reviews[0].set()
        assert editor_started.wait(8)
        assert env.pipeline.source_path(session.session_id) is None
        assert counts == {"coverage": 1, "correctness": 1}
        release_editor.set()
        assert review_started[1].wait(8)
        assert env.pipeline.source_path(session.session_id) is None
        release_reviews[1].set()
        env.wait_until(
            lambda: env.pipeline.status(session.session_id)["phase"] == "published"
        )
        assert counts == {"coverage": 2, "correctness": 2}
        assert len(writer_ids) == 2 and len(set(writer_ids)) == 1
        assert all(len(set(ids)) == 1 for ids in reviewer_ids.values())
        assert env.pipeline.status(session.session_id)["review_status"] == "passed"
        for filename, _ in env.pipeline.note_files("final").values():
            text = (
                tmp_path / "sessions" / session.session_id / "notes" / filename
            ).read_text()
            assert "The theorem includes its required condition" in text
            assert "Draft missing" not in text
        assert env.agent.peak_active == 2


def test_final_tasks_keep_session_sources_when_selection_changes(tmp_path):
    both_writers = threading.Barrier(3)
    release_writers = threading.Event()
    writer_sources = {}

    def run(session, instruction):
        sid = session.meta["lecture_session"]
        source = session.workspace.read_lines(SOURCE_FILENAME, 1, -1)
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
            env.hub.seal_text(session.session_id, "transcript", 10, final=True)
            lectures.append((session.session_id, marker))
            assert env.pipeline.start(session.session_id)
        both_writers.wait(8)
        third = env.hub.start_session(meta={"title": "Unrelated selected lecture"})
        third.transcript.append("Do not copy this third lecture", start=0, end=1)
        env.hub.set_notes_session(third.session_id)
        release_writers.set()
        env.wait_until(
            lambda: all(
                env.pipeline.status(sid)["phase"] == "published" for sid, _ in lectures
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
            for filename, _ in env.pipeline.note_files("final").values():
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
        env.hub.seal_text(session.session_id, "transcript", 10, final=True)
        assert env.pipeline.start(session.session_id)
        env.wait_until(
            lambda: env.pipeline.status(session.session_id)["phase"] == "published"
        )
        expected = env.pipeline.status(session.session_id)["review_status"]
    with note_pipeline(
        tmp_path,
        lambda session, instruction: write_notes(session, "Unexpected regeneration"),
    ) as restored:
        assert restored.pipeline.status(session.session_id)["review_status"] == expected
        assert restored.pipeline.status(session.session_id)["phase"] == "published"


def test_manual_entries_keep_incremental_occurrences(tmp_path):
    sources = []

    def run(session, instruction):
        source = session.workspace.read_lines("source.md", 1, -1)
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


def test_empty_writer_continues_in_same_session(tmp_path):
    calls = []

    def run(session, instruction):
        calls.append(session.id)
        if len(calls) == 1:
            return note_result("done")
        return write_notes(session, "Recovered theorem with conditions")

    with note_pipeline(
        tmp_path, run, settings={"instant-summarize.enabled": False}
    ) as env:
        session = env.hub.start_session()
        session.transcript.append("Theorem with conditions", start=0, end=10)
        env.hub.end_session()
        env.hub.seal_text(session.session_id, "transcript", 10, final=True)
        env.pipeline.start(session.session_id)
        env.wait_until(
            lambda: env.pipeline.status(session.session_id)["phase"] == "published"
        )
        assert len(calls) == 2 and calls[0] == calls[1]
        assert (
            "Recovered theorem"
            in (
                tmp_path
                / "sessions"
                / session.session_id
                / "notes"
                / "note_knowledge.md"
            ).read_text()
        )


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
    pipeline.ctx = SimpleNamespace(data_dir=tmp_path, ui=SimpleNamespace(refresh=lambda _: None))
    pipeline.closed = False
    pipeline.settings = FakeSettings()
    pipeline.sessions = {}
    sid = "lecture"
    state = pipeline._state(sid)
    state["chunks"] = [{"index": index, "end": (index + 1) * 10, "state": "done"} for index in range(3)]
    for index in range(3):
        directory = pipeline._directory(sid) / "chunks" / str(index)
        directory.mkdir(parents=True)
        for name, header in NOTE_HEADERS.items():
            body = "## 作业\n\n完成练习 1．\n" if index == 1 and name == "note_notice.md" else ""
            (directory / name).write_text(header + body)
    pipeline._publish_preview(sid)
    text = (tmp_path / "sessions" / sid / "notes" / "instant_note_notice.md").read_text()
    assert "课堂讲义：通知" not in text
    assert text.count("# 通知\n") == 1
    assert "## 作业\n\n完成练习 1．" in text
