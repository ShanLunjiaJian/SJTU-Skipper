"""Schedule session-bound note drafts and whole-lecture publication."""

from __future__ import annotations

import dataclasses
import json
import math
import os
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any
from pathlib import Path

from core.file_access import publication_lock
from core.agent_types import AgentRunResult
from core.plugin import Plugin
from core.settings_spec import SettingsSpec
from plugins.agent.workspace import FileWorkspace
from .prompts import (
    WRITER,
    COVERAGE,
    CORRECTNESS,
    REVIEW_SYSTEM,
    RETRY,
    REVISION,
    REVIEW_FORMAT,
)
from plugins.reader.qa import parse_blocks, strip_blocks
from .material import chunk_material, collect_material, digest, render
from .prompts import CHUNK, KINDS, LABELS, NOTE_HEADERS, SOURCE_FILENAME, SYSTEM
from .worker import session_state, restore_workspace


class NotesPipelineService:
    def __init__(self, pipeline):
        self._pipeline = pipeline

    def start(self, session_id=""):
        return self._pipeline.start(session_id)

    def status(self, session_id=""):
        return self._pipeline.status(session_id)

    def reset(self, session_id="", regenerate=False):
        return self._pipeline.reset(session_id, regenerate)

    def draft_paths(self, session_id=""):
        return self._pipeline.draft_paths(session_id)

    def source_path(self, session_id=""):
        return self._pipeline.source_path(session_id)

    def note_files(self, kind):
        return self._pipeline.note_files(kind)

    def running_tasks(self):
        return self._pipeline.running_tasks()


class NotesPipelinePlugin(Plugin):
    id = "notes-pipeline"
    requires = ("settings", "llm-agent", "llm")
    optional_requires = ("stt", "vlm")
    provides = ("notes-pipeline",)

    def setup(self, ctx):
        self.ctx = ctx
        self.settings = ctx.require("settings")
        self.agent = ctx.require("agent")
        for key, default, label in (
            ("chunk_seconds", 600, "片段秒数"),
            ("overlap_seconds", 60, "前文秒数"),
            ("concurrency", 8, "笔记并发数"),
        ):
            self.settings.register(
                SettingsSpec(
                    key=f"notes.{key}",
                    default=default,
                    type="int",
                    label=label,
                    group="Notes",
                )
            )
        self.condition = threading.Condition(threading.RLock())
        self.sessions = {}
        self.queue = deque()
        self.active = {}
        self.agent_tasks = {}
        self.workers = {}
        self.reset_locks = {}
        self.closed = False
        self.limit = max(1, int(self.setting("notes.concurrency", 8)))
        self.pool = ThreadPoolExecutor(
            max_workers=self.limit, thread_name_prefix="notes"
        )
        self.agent_running = 0
        for path in ctx.data_dir.glob("sessions/*/note_work/state.json"):
            self._state(path.parent.parent.name)
        ctx.register_service(self.id, NotesPipelineService(self))
        ctx.ui.register_task_provider(self.running_tasks, plugin_id=self.id)
        for event in ("stream.sealed", "stream.appended", "session.stopped"):
            ctx.events.on(event, self._event, plugin_id=self.id)
        for event in ("agent.message", "agent.tool_call", "agent.tool_result"):
            ctx.events.on(event, self._agent_event, plugin_id=self.id)
        self.coordinator = threading.Thread(
            target=self._coordinate, name="notes-coordinator", daemon=True
        )
        self.coordinator.start()

    def setting(self, key, default):
        return self.settings.get(key, default)

    def _sid(self, sid=""):
        return (
            sid
            or self.ctx.streams.notes_session_id
            or self.ctx.streams.current.session_id
        )

    def _directory(self, sid):
        return self.ctx.data_dir / "sessions" / sid / "note_work"

    def _state(self, sid):
        if sid not in self.sessions:
            path = self._directory(sid) / "state.json"
            state: dict[str, Any] = (
                json.loads(path.read_text(encoding="utf-8"))
                if path.exists()
                else {
                    "session_id": sid,
                    "source_digest": "",
                    "chunks": [],
                    "published_end": 0.0,
                    "finalize_requested": False,
                    "phase": "collecting",
                    "error": "",
                }
            )
            for chunk in state["chunks"]:
                if chunk["state"] == "running":
                    chunk["state"] = "pending"
                if chunk["state"] == "done":
                    root = self._directory(sid) / "chunks" / str(chunk["index"])
                    if not all((root / name).exists() for name in NOTE_HEADERS):
                        chunk["state"] = "pending"
            state.setdefault("review_status", "pending")
            state.setdefault("deleted", False)
            state["errors"] = [
                {"chunk": chunk["index"], "error": chunk["error"]}
                for chunk in state["chunks"]
                if chunk["state"] == "failed"
            ]
            self.sessions[sid] = state
        return self.sessions[sid]

    def _save(self, state):
        directory = self._directory(state["session_id"])
        directory.mkdir(parents=True, exist_ok=True)
        fields = (
            "session_id",
            "source_digest",
            "chunks",
            "published_end",
            "finalize_requested",
            "phase",
            "error",
            "review_status",
            "deleted",
        )
        path = directory / "state.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({key: state[key] for key in fields}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def _event(self, event):
        sid = str(event.get("session_id", ""))
        if not sid or sid == "boot":
            return
        with self.condition:
            state = self._state(sid)
            if event.get("error"):
                state["errors"].append(
                    {"source": event.get("stream"), **event.get("error")}
                )
            self.condition.notify_all()

    def start(self, session_id="") -> bool:

        sid = self._sid(session_id)
        if not sid or sid == "boot":
            return False
        records = collect_material(self.ctx, sid)
        session = self.ctx.streams.get_session(sid)
        if not records and (session is None or session.ended_at is not None):
            return False
        with self.condition:
            if self.closed:
                return False
            state = self._state(sid)
            if state["phase"] == "resetting":
                return False
            state["deleted"] = False
            if state["phase"] in ("published", "failed", "deleted"):
                state["phase"] = "collecting"
            state["finalize_requested"] = True
            self._save(state)
            self.condition.notify_all()
        return True

    def reset(self, session_id="", regenerate=False):
        sid = self._sid(session_id)
        with self.condition:
            reset_lock = self.reset_locks.setdefault(sid, threading.Lock())
        with reset_lock:
            with self.condition:
                state = self._state(sid)
                state["deleted"] = True
                state["phase"] = "resetting"
                state["finalize_requested"] = False
                self.queue = deque(task for task in self.queue if task[0] != sid)
                workers = [
                    process for owner, process in self.workers.values() if owner == sid
                ]
                for process in workers:
                    if process.poll() is None:
                        process.kill()
                self._save(state)
                self.condition.notify_all()
            for process in workers:
                process.wait()
            with self.condition:
                self.condition.wait_for(
                    lambda: (
                        not any(
                            task["session_id"] == sid for task in self.active.values()
                        )
                        and not any(owner == sid for owner, _ in self.workers.values())
                    )
                )
                notes = self.ctx.data_dir / "sessions" / sid / "notes"
                with publication_lock(notes):
                    for kind in ("instant", "final"):
                        for name, _ in self.note_files(kind).values():
                            (notes / name).unlink(missing_ok=True)
                    (notes.parent / SOURCE_FILENAME).unlink(missing_ok=True)
                state.update(
                    chunks=[],
                    source_digest="",
                    published_end=0.0,
                    phase="collecting" if regenerate else "deleted",
                    error="",
                    errors=[],
                    review_status="pending",
                    deleted=not regenerate,
                )
                self._save(state)
                if regenerate:
                    self.start(sid)
                self.condition.notify_all()
            self.ctx.ui.refresh("notes.reset")
        return True

    def status(self, session_id=""):
        sid = self._sid(session_id)
        with self.condition:
            state = self._state(sid)
            return {
                "session_id": sid,
                "phase": state["phase"],
                "pending_chunks": sum(c["state"] == "pending" for c in state["chunks"]),
                "running_chunks": sum(c["state"] == "running" for c in state["chunks"]),
                "published_end": state["published_end"],
                "review_status": state["review_status"],
                "completed_chunks": sum(
                    c["state"] in ("done", "empty") for c in state["chunks"]
                ),
                "total_chunks": len(state["chunks"]),
                "tasks": [
                    dict(task)
                    for task in self.agent_tasks.values()
                    if sid == task["session_id"]
                ],
                "error": state["error"],
                "errors": list(state["errors"]),
            }

    def note_files(self, kind):
        if kind not in ("instant", "final"):
            raise ValueError(kind)
        prefix = "instant_note" if kind == "instant" else "note"
        return {key: (f"{prefix}_{key}.md", LABELS[key]) for key in KINDS}

    def draft_paths(self, session_id=""):
        directory = self.ctx.data_dir / "sessions" / self._sid(session_id) / "notes"
        return {
            key: directory / name
            for key, (name, _) in self.note_files("instant").items()
            if (directory / name).exists()
        }

    def source_path(self, session_id=""):
        path = self.ctx.data_dir / "sessions" / self._sid(session_id) / SOURCE_FILENAME
        return path if path.exists() else None

    def running_tasks(self):
        with self.condition:
            tasks = []
            for task in self.active.values():
                sid = task["session_id"]
                phase = (
                    "chunking"
                    if task["phase"] == "chunking"
                    else self._state(sid)["phase"]
                )
                elapsed = max(0, int(time.monotonic() - task["started"]))
                tasks.append(
                    {
                        **task,
                        "phase": phase,
                        "label": f"笔记 {sid}",
                        "detail": f"{phase}；片段 {task['chunk']}；{elapsed} 秒；审阅 {self._state(sid)['review_status']}",
                    }
                )
            tasks.extend(
                {
                    "id": f"notes:{sid}:{kind}:{index}",
                    "session_id": sid,
                    "label": f"笔记 {sid}",
                    "detail": f"排队；{kind}；片段 {index}",
                    "phase": kind,
                    "chunk": index,
                }
                for sid, kind, index in self.queue
            )
            tasks.extend(
                {
                    **task,
                    "detail": f"步骤 {task['agent_step']}；{max(0, int(time.monotonic() - task['started']))} 秒",
                }
                for task in self.agent_tasks.values()
            )
            return tasks

    def _readiness(self, sid, records):
        session = self.ctx.streams.get_session(sid)
        if session is None:
            return math.inf, True
        states = []
        for service, stream in (("stt", "transcript"), ("vlm", "visual")):
            if self.ctx.service(service) is not None:
                progress = self.ctx.streams.text_progress(sid, stream)
                if progress.enabled:
                    states.append(progress)
        if not states:
            return (
                (math.inf, True)
                if session.input_done or session.ended_at is not None
                else (0.0, False)
            )
        return min(
            (math.inf if state.final else state.before) for state in states
        ), all(state.final for state in states) and (
            session.input_done or session.ended_at is not None
        )

    def _schedule(self, sid):
        state = self._state(sid)
        if state.get("deleted"):
            return
        records = collect_material(self.ctx, sid)
        before, final = self._readiness(sid, records)
        seconds = max(1, float(self.setting("notes.chunk_seconds", 600)))
        end = max(
            (
                float(record.get("end") or record.get("start") or 0)
                for record in records
            ),
            default=0,
        )
        session = self.ctx.streams.get_session(sid)
        if session is not None:
            end = max(end, session.duration)
        enabled = bool(self.setting("instant-summarize.enabled", True))
        manual = [record for record in records if record.get("start") is None]
        manual_before = max(
            (chunk.get("manual_end", 0) for chunk in state["chunks"]), default=0
        )
        count = int(end // seconds)
        if final and (enabled or state["finalize_requested"]) and end > count * seconds:
            count += 1
        if final and records and not count:
            count = 1
        if manual and not end:
            count = len(state["chunks"]) + int(len(manual) > manual_before)
        if enabled:
            for index in range(count):
                start, finish = index * seconds, min((index + 1) * seconds, end)
                if manual and not end:
                    start = finish = 0.0
                if not final and before < finish and end:
                    break
                core, context = chunk_material(
                    records,
                    start,
                    finish,
                    float(self.setting("notes.overlap_seconds", 60)),
                )
                existing = next(
                    (c for c in state["chunks"] if c["index"] == index), None
                )
                manual_start = (
                    existing.get("manual_start", 0) if existing else manual_before
                )
                manual_end = existing.get("manual_end", 0) if existing else len(manual)
                core += manual[manual_start:manual_end]
                input_digest = digest(core + context)
                if (
                    existing
                    and existing["input_digest"] != input_digest
                    and existing["state"] != "running"
                ):
                    existing.update(
                        state="pending", input_digest=input_digest, error="", end=finish
                    )
                if existing is None:
                    existing = {
                        "index": index,
                        "start": start,
                        "end": finish,
                        "input_digest": input_digest,
                        "state": "pending",
                        "attempts": 0,
                        "error": "",
                        "manual_start": manual_start,
                        "manual_end": manual_end,
                    }
                    state["chunks"].append(existing)
                    manual_before = manual_end
                task = (sid, "chunk", index)
                if existing["state"] == "pending" and task not in self.queue:
                    self.queue.append(task)
        state["source_digest"] = digest(records)
        if (
            final
            and state["finalize_requested"]
            and all(c["state"] in ("done", "empty", "failed") for c in state["chunks"])
        ):
            task = (sid, "final", -1)
            if (
                records
                and task not in self.queue
                and not any(
                    t["session_id"] == sid and t["phase"] != "chunking"
                    for t in self.active.values()
                )
                and state["phase"] not in ("published", "failed")
            ):
                self.queue.appendleft(task)
        self._save(state)

    def _coordinate(self):
        while True:
            with self.condition:
                if self.closed:
                    return
                ids = list(self.sessions)
            for sid in ids:
                try:
                    with self.condition:
                        self._schedule(sid)
                except Exception as exc:
                    with self.condition:
                        self._state(sid)["errors"].append({"error": str(exc)})
            with self.condition:
                while self.queue and len(self.active) < self.limit:
                    sid, kind, index = self.queue.popleft()
                    token = f"{sid}:{kind}:{index}"
                    state = self._state(sid)
                    if state.get("deleted"):
                        continue
                    if kind == "chunk":
                        state["chunks"][index]["state"] = "running"
                        state["phase"] = "chunking"
                    else:
                        state["phase"] = "writing"
                    self.active[token] = {
                        "id": token,
                        "session_id": sid,
                        "phase": "chunking" if kind == "chunk" else "writing",
                        "chunk": index,
                        "started": time.monotonic(),
                        "agent_step": 0,
                    }
                    records = collect_material(self.ctx, sid)
                    self.pool.submit(self._execute, token, sid, kind, index, records)
                self.condition.wait(0.1)

    def _execute(self, token, sid, kind, index, records):
        try:
            if kind == "chunk":
                self._chunk(sid, index, records)
            else:
                self._final(sid, records)
        except Exception as exc:
            with self.condition:
                if self._state(sid).get("deleted"):
                    return
                if self.closed:
                    if kind == "chunk":
                        self._state(sid)["chunks"][index]["state"] = "pending"
                    self._save(self._state(sid))
                    return
                state = self._state(sid)
                state["errors"].append({"chunk": index, "error": str(exc)})
                state["error"] = str(exc)
                if kind == "chunk":
                    state["chunks"][index].update(state="failed", error=str(exc))
                else:
                    state["phase"] = "failed"
                self._save(state)
        finally:
            with self.condition:
                self.active.pop(token, None)
                self.condition.notify_all()

    def _agent_event(self, event):
        with self.condition:
            task = self.agent_tasks.get(str(event.get("session_id", "")))
            if task is not None:
                task["agent_step"] = int(event.get("step", 0))

    def _check_running(self, sid):
        if self.closed or self._state(sid).get("deleted"):
            raise RuntimeError("Note generation cancelled")

    @contextmanager
    def _agent_slot(self, sid):
        with self.condition:
            self.condition.wait_for(
                lambda: (
                    self.closed
                    or self._state(sid).get("deleted")
                    or self.agent_running < self.limit
                )
            )
            self._check_running(sid)
            self.agent_running += 1
        try:
            yield
        finally:
            with self.condition:
                self.agent_running -= 1
                self.condition.notify_all()

    def _lecture_context(self, sid):
        session = self.ctx.streams.get_session(sid)
        if session is not None:
            meta = dict(session.meta)
        else:
            path = self.ctx.data_dir / "sessions" / sid / "meta.json"
            saved = (
                json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            )
            meta = saved.get("meta", saved)
        return (
            "## 目标课堂\n会话："
            + sid
            + "\n课堂元信息："
            + json.dumps(meta, ensure_ascii=False)
            + "\n"
        )

    def _agent_run(self, sid, session_id, instruction):
        with self._agent_slot(sid):
            session = self.agent.get_session(session_id)
            payload = {
                "state": session_state(session),
                "provider": dataclasses.asdict(self.ctx.llm.config_for("agent")),
                "system_prompt": self.agent._build_system_prompt(session),
                "metadata": self._lecture_context(sid),
                "trace_dir": str(self.ctx.data_dir / "llm"),
                "instruction": self._lecture_context(sid) + "\n" + instruction,
            }
            source_root = str(Path(__file__).resolve().parents[2])
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.pathsep.join(
                [source_root, environment.get("PYTHONPATH", "")]
            )
            with self.condition:
                self._check_running(sid)
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "from plugins.notes_pipeline.worker import main; main()",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    env=environment,
                )
                self.workers[session_id] = (sid, process)
                self.agent_tasks[session_id] = {
                    "id": f"notes-agent:{session_id}",
                    "session_id": sid,
                    "label": session.name,
                    "detail": "执行中",
                    "agent_step": 0,
                    "started": time.monotonic(),
                }
            try:
                json.dump(payload, process.stdin, ensure_ascii=False)
                process.stdin.close()
                process.stdin = None
                completed = None
                failure = ""
                for line in process.stdout:
                    message = json.loads(line)
                    with self.condition:
                        self._check_running(sid)
                        if message["type"] == "event":
                            data = message["data"]
                            if message["name"] == "llm.response":
                                from llm.types import Usage

                                llm = self.ctx.service("llm")
                                data["usage_totals"] = llm._record_usage(
                                    data["model"] or session.model,
                                    Usage(**data["usage"]),
                                )
                            self.ctx.events.emit(message["name"], **data)
                        elif message["type"] == "refresh":
                            if session.meta.get("role") == "writer" and message["reason"].startswith("agent.file:"):
                                self._publish_lecture(sid, session.workspace, draft=True)
                            self.ctx.ui.refresh(message["reason"])
                        elif message["type"] == "result":
                            completed = message
                        elif message["type"] == "error":
                            failure = message["error"]
                return_code = process.wait()
                with self.condition:
                    self._check_running(sid)
                    if return_code or completed is None:
                        raise RuntimeError(
                            failure or f"Note worker exited with status {return_code}"
                        )
                    values = completed["state"]
                    for name, value in values["session"].items():
                        setattr(
                            session,
                            name,
                            tuple(value) if name == "extra_instructions" else value,
                        )
                    session.workspace = restore_workspace(values["workspace"])
                    return AgentRunResult(**completed["result"])
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdin is not None:
                    process.stdin.close()
                process.stdout.close()
                with self.condition:
                    self.workers.pop(session_id, None)
                    self.agent_tasks.pop(session_id, None)
                    self.condition.notify_all()

    def _run_agent(
        self,
        sid,
        name,
        workspace,
        instruction,
        *,
        prefix="instant-summarize",
        readonly=False,
    ):
        session = self.agent.create_session(
            name,
            workspace=workspace,
            system_prompt=self.setting(f"{prefix}.system_prompt", "") or SYSTEM,
            tools=[
                (tool, {})
                for tool in (
                    ("info", "read", "search")
                    if readonly
                    else ("info", "read", "search", "write", "replace", "append")
                )
            ],
            model=self.setting(f"{prefix}.model", "") or None,
            max_steps=int(self.setting(f"{prefix}.max_steps", 40)),
            max_tool_output=int(self.setting(f"{prefix}.max_tool_output", 200000)),
            meta={"internal": True, "role": "notes", "lecture_session": sid},
        )
        try:
            result = self._agent_run(sid, session.id, instruction)
            if result.error:
                raise RuntimeError(result.error)
            return result
        finally:
            self.agent.close(session.id)

    def _chunk(self, sid, index, records):
        with self.condition:
            chunk = dict(self._state(sid)["chunks"][index])
            self._check_running(sid)
        core, context = chunk_material(
            records,
            chunk["start"],
            chunk["end"],
            float(self.setting("notes.overlap_seconds", 60)),
        )
        manual = [record for record in records if record.get("start") is None]
        core += manual[chunk.get("manual_start", 0) : chunk.get("manual_end", 0)]
        if not core:
            with self.condition:
                self._state(sid)["chunks"][index]["state"] = "empty"
                self._publish_preview(sid)
                self._save(self._state(sid))
            return
        root = self._directory(sid) / "chunks" / str(index)
        root.mkdir(parents=True, exist_ok=True)
        (root / "source.md").write_text(render(core), encoding="utf-8")
        (root / "context.md").write_text(render(context), encoding="utf-8")
        (root / "visual_history.md").write_text(
            render(
                [
                    r
                    for r in records
                    if r["stream"] == "visual"
                    and float(r.get("start") or 0) < chunk["start"]
                ]
            ),
            encoding="utf-8",
        )
        attempts = max(1, int(self.setting("instant-summarize.retry_max", 2)) + 1)
        last_error = None
        for attempt in range(attempts):
            directory = root / f"attempt-{attempt}"
            directory.mkdir(exist_ok=True)
            files = {
                name: root / name
                for name in ("source.md", "context.md", "visual_history.md")
            }
            for name, header in NOTE_HEADERS.items():
                files[name] = directory / name
                files[name].write_text(header, encoding="utf-8")
            workspace = FileWorkspace(
                files, readonly=("source.md", "context.md", "visual_history.md")
            )
            try:
                self._run_agent(
                    sid,
                    f"{sid} 片段 {index}",
                    workspace,
                    CHUNK + ("\n" + RETRY if attempt else ""),
                )
                self._validate_outputs(directory)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                with self.condition:
                    self._check_running(sid)
        if last_error:
            raise last_error
        with self.condition:
            self._check_running(sid)
            target = self._state(sid)["chunks"][index]
            target.update(state="done", attempts=attempt + 1)
            for name in NOTE_HEADERS:
                os.replace(directory / name, root / name)
            self._publish_preview(sid)
            self._save(self._state(sid))

    def _validate_outputs(self, directory):
        bodies = []
        for name in NOTE_HEADERS:
            text = (directory / name).read_text(encoding="utf-8")
            bodies.append(
                any(
                    line.strip() and not line.lstrip().startswith("#")
                    for line in text.splitlines()
                )
            )
        if not any(bodies):
            raise RuntimeError("The note agent produced no body text")

    def _publish_preview(self, sid):
        if (
            self.closed
            or self._state(sid).get("deleted")
            or not self.setting("instant-summarize.enabled", True)
        ):
            return
        state = self._state(sid)
        chunks = []
        for chunk in sorted(state["chunks"], key=lambda c: c["index"]):
            if chunk["state"] not in ("done", "empty"):
                break
            chunks.append(chunk)
        notes = self.ctx.data_dir / "sessions" / sid / "notes"
        notes.mkdir(parents=True, exist_ok=True)
        with publication_lock(notes):
            for kind in KINDS:
                body = []
                for chunk in chunks:
                    path = (
                        self._directory(sid)
                        / "chunks"
                        / str(chunk["index"])
                        / f"note_{kind}.md"
                    )
                    if path.exists():
                        text = strip_blocks(path.read_text(encoding="utf-8")).strip()
                        heading = NOTE_HEADERS[f"note_{kind}.md"].strip()
                        lines = text.splitlines()
                        if lines and lines[0].strip() == heading:
                            text = "\n".join(lines[1:]).strip()
                        if text:
                            body.append(text)
                path = notes / f"instant_note_{kind}.md"
                previous = path.read_text(encoding="utf-8") if path.exists() else ""
                seen = set()
                qa_blocks = []
                for block in parse_blocks(previous):
                    if block.id not in seen:
                        seen.add(block.id)
                        qa_blocks.append(previous[block.start : block.end])
                content = f"# {LABELS[kind]}\n\n> 未审阅\n\n" + "\n\n".join(body)
                if qa_blocks:
                    content += "\n\n## 问答\n\n" + "\n\n".join(qa_blocks)
                temporary = path.with_suffix(".tmp")
                temporary.write_text(
                    content,
                    encoding="utf-8",
                )
                os.replace(temporary, path)
        state["published_end"] = chunks[-1]["end"] if chunks else 0
        self.ctx.ui.refresh("notes.preview")

    def _publish_lecture(self, sid, workspace, *, draft=False):
        notes = self.ctx.data_dir / "sessions" / sid / "notes"
        notes.mkdir(parents=True, exist_ok=True)
        with self.condition, publication_lock(notes):
            self._check_running(sid)
            for name in NOTE_HEADERS:
                destination = notes / name
                latest = destination.read_text(encoding="utf-8") if destination.exists() else ""
                seen = set()
                blocks = []
                for block in parse_blocks(latest):
                    if block.id not in seen:
                        seen.add(block.id)
                        blocks.append(latest[block.start:block.end])
                body = strip_blocks(workspace.path(name).read_text(encoding="utf-8"))
                if draft:
                    body = "> 生成中，未审阅\n\n" + body
                if blocks:
                    body += "\n\n## 问答\n\n" + "\n\n".join(blocks)
                temporary = destination.with_suffix(".tmp")
                temporary.write_text(body, encoding="utf-8")
                os.replace(temporary, destination)
            source = notes.parent / SOURCE_FILENAME
            temporary = source.with_suffix(".tmp")
            temporary.write_bytes(workspace.path(SOURCE_FILENAME).read_bytes())
            os.replace(temporary, source)

    def _final(self, sid, records):
        with self.condition:
            self._check_running(sid)
        generation = digest(records)
        directory = self._directory(sid) / "final" / generation
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / SOURCE_FILENAME
        errors = self._state(sid)["errors"]
        gap_notice = (
            "# 来源缺口\n\n" + json.dumps(errors, ensure_ascii=False) + "\n\n"
            if errors
            else ""
        )
        source.write_text(gap_notice + render(records), encoding="utf-8")
        files = {SOURCE_FILENAME: source}
        index_lines = []
        for chunk in self._state(sid)["chunks"]:
            if chunk["state"] != "done":
                index_lines.append(
                    f"Chunk {chunk['index']}: {chunk['state']} {chunk['error']}"
                )
                continue
            for kind in KINDS:
                name = f"chunk_{chunk['index']}_{kind}.md"
                original = (
                    self._directory(sid)
                    / "chunks"
                    / str(chunk["index"])
                    / f"note_{kind}.md"
                )
                frozen = directory / name
                frozen.write_text(
                    strip_blocks(original.read_text(encoding="utf-8")), encoding="utf-8"
                )
                files[name] = frozen
                index_lines.append(name)
        index_path = directory / "index.md"
        index_path.write_text("\n".join(index_lines), encoding="utf-8")
        files["index.md"] = index_path
        readonly = list(files)
        for name, header in NOTE_HEADERS.items():
            files[name] = directory / name
            files[name].write_text(header, encoding="utf-8")
        workspace = FileWorkspace(files, readonly=readonly)
        writer = self.agent.create_session(
            f"{sid} 整课写作",
            workspace=workspace,
            system_prompt=self.setting("summarize.system_prompt", "") or SYSTEM,
            tools=[
                (tool, {})
                for tool in ("info", "read", "search", "write", "replace", "append")
            ],
            model=self.setting("summarize.model", "") or None,
            max_steps=int(self.setting("summarize.max_steps", 0)),
            max_tokens=int(self.setting("summarize.max_tokens", 32000)),
            max_tool_output=int(self.setting("summarize.max_tool_output", 200000)),
            max_history_chars=int(self.setting("summarize.max_history_chars", 0)),
            meta={"internal": True, "role": "writer", "lecture_session": sid},
        )
        reviewers = []
        try:
            result = self._agent_run(sid, writer.id, WRITER)
            if result.error:
                raise RuntimeError(result.error)
            try:
                self._validate_outputs(directory)
            except RuntimeError:
                result = self._agent_run(sid, writer.id, RETRY)
                if result.error:
                    raise RuntimeError(result.error)
                self._validate_outputs(directory)
            review_enabled = bool(self.setting("summarize.review_enabled", True))
            review_status = "disabled"
            if review_enabled:
                review_files = {
                    name: files[name] for name in (SOURCE_FILENAME, *NOTE_HEADERS)
                }
                for label in ("coverage", "correctness"):
                    reviewers.append(
                        self.agent.create_session(
                            f"{sid} {label}",
                            workspace=FileWorkspace(
                                review_files, readonly=review_files
                            ),
                            system_prompt=self.setting("summarize.reviewer_prompt", "")
                            or REVIEW_SYSTEM,
                            tools=[(tool, {}) for tool in ("info", "read", "search")],
                            model=self.setting("summarize.review_model", "")
                            or self.setting("summarize.model", "")
                            or None,
                            max_steps=int(
                                self.setting("summarize.review_max_steps", 0)
                            ),
                            final_answer_on_limit=True,
                            max_tokens=int(
                                self.setting("summarize.review_max_tokens", 32000)
                            ),
                            max_tool_output=int(
                                self.setting("summarize.max_tool_output", 200000)
                            ),
                            max_history_chars=int(
                                self.setting("summarize.max_history_chars", 0)
                            ),
                            meta={
                                "internal": True,
                                "role": label,
                                "lecture_session": sid,
                            },
                        )
                    )
                previous = None
                max_rounds = int(self.setting("summarize.review_max_rounds", 0))
                round_index = 0
                while not max_rounds or round_index < max_rounds:
                    round_index += 1
                    with self.condition:
                        self._check_running(sid)
                        self._state(sid)["phase"] = "reviewing"
                    with ThreadPoolExecutor(
                        max_workers=min(2, self.limit),
                        thread_name_prefix="notes-review",
                    ) as reviews:
                        futures = [
                            reviews.submit(self._agent_run, sid, reviewer.id, prompt)
                            for reviewer, prompt in zip(
                                reviewers, (COVERAGE, CORRECTNESS)
                            )
                        ]
                        results = [future.result() for future in futures]
                    for result in results:
                        if result.error:
                            raise RuntimeError(result.error)
                    for index, result in enumerate(results):
                        if (
                            "REVIEW: PASS" not in result.final_message
                            and "REVIEW: FAIL" not in result.final_message
                        ):
                            results[index] = self._agent_run(
                                sid, reviewers[index].id, REVIEW_FORMAT
                            )
                            if results[index].error:
                                raise RuntimeError(results[index].error)
                    if all(
                        "REVIEW: PASS" in result.final_message
                        and "REVIEW: FAIL" not in result.final_message
                        for result in results
                    ):
                        review_status = "passed"
                        break
                    feedback = "\n\n".join(result.final_message for result in results)
                    if feedback == previous:
                        break
                    if max_rounds and round_index >= max_rounds:
                        break
                    previous = feedback
                    before = [files[name].read_bytes() for name in NOTE_HEADERS]
                    with self.condition:
                        self._check_running(sid)
                        self._state(sid)["phase"] = "revising"
                    result = self._agent_run(
                        sid,
                        writer.id,
                        REVISION.format(review=feedback),
                    )
                    if result.error:
                        raise RuntimeError(result.error)
                    if before == [files[name].read_bytes() for name in NOTE_HEADERS]:
                        break
                if review_status != "passed":
                    with self.condition:
                        self._state(sid)["review_status"] = "failed"
                    raise RuntimeError("Lecture review did not pass")
            self._publish_lecture(sid, workspace)
            with self.condition:
                self._check_running(sid)
                state = self._state(sid)
                state.update(
                    phase="published",
                    review_status=review_status,
                    finalize_requested=False,
                )
                self._save(state)
                self.ctx.ui.refresh("notes.published")
        finally:
            self.agent.close(writer.id)
            for reviewer in reviewers:
                self.agent.close(reviewer.id)

    def teardown(self):
        with self.condition:
            self.closed = True
            self.queue.clear()
            workers = [process for _, process in self.workers.values()]
            for process in workers:
                if process.poll() is None:
                    process.kill()
            self.condition.notify_all()
        for process in workers:
            process.wait()
        self.coordinator.join()
        self.pool.shutdown(wait=True, cancel_futures=True)
