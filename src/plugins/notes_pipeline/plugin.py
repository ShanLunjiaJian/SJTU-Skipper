"""Schedule session-bound note drafts and whole-lecture publication."""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

from core.file_access import publication_lock
from core.plugin import Plugin
from core.settings_spec import SettingsSpec
from core.workspace import FileWorkspace
from core.note_document import parse_blocks, strip_blocks

from .material import chunk_material, collect_material, digest, render
from .prompts import (
    CHUNK,
    REVIEW,
    KINDS,
    LABELS,
    NOTE_HEADERS,
    REVIEW_SYSTEM,
    SOURCE_FILENAME,
    SYSTEM,
    WRITER,
)


class NotesPipelineService:
    def __init__(self, pipeline):
        self._pipeline = pipeline

    def start(self, session_id=""):
        return self._pipeline.start(session_id)

    def finalize(self, session_id=""):
        return self._pipeline.finalize(session_id)

    def status(self, session_id=""):
        return self._pipeline.status(session_id)

    def stop(self, session_id=""):
        return self._pipeline.stop(session_id)

    def delete(self, session_id=""):
        return self._pipeline.delete(session_id)

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
                    description="修改后重启服务生效" if key == "concurrency" else "",
                )
            )
        for key, default, kind, label, group in (
            ("summarize.system_prompt", SYSTEM, "string", "system_prompt", "Summarize"),
            (
                "summarize.reviewer_prompt",
                REVIEW_SYSTEM,
                "string",
                "reviewer_prompt",
                "Summarize",
            ),
            ("summarize.review_enabled", True, "bool", "review_enabled", "Summarize"),
            ("summarize.review_max_rounds", 0, "int", "review_max_rounds", "Summarize"),
            (
                "instant-summarize.system_prompt",
                SYSTEM,
                "string",
                "系统提示词",
                "Instant Summarize",
            ),
            (
                "instant-summarize.enabled",
                True,
                "bool",
                "分段笔记",
                "Instant Summarize",
            ),
        ):
            self.settings.register(
                SettingsSpec(
                    key=key,
                    default=default,
                    type=kind,
                    label=label,
                    group=group,
                )
            )
        self.condition = threading.Condition(threading.RLock())
        self.sessions = {}
        self.queue = deque()
        self.active = {}
        self.agent_tasks = {}
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
        ctx.ui.register_action(
            "notes-pipeline.stop",
            lambda payload: {
                "ok": self.stop(str(payload.get("session_id", ""))),
                "refresh": True,
            },
            plugin_id=self.id,
        )
        for event in (
            "session.started",
            "stream.sealed",
            "stream.appended",
            "session.stopped",
        ):
            ctx.events.on(event, self._event, plugin_id=self.id)
        for event in (
            "agent.execution",
            "agent.file",
            "agent.reviewer",
        ):
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
            restored = path.exists()
            for chunk in state["chunks"]:
                if chunk["state"] in ("running", "pending") and restored:
                    chunk["state"] = "cancelled"
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
            if restored:
                state["finalize_requested"] = False
                if state["phase"] not in ("published", "deleted"):
                    state["phase"] = "cancelled"
                state["restart_cancelled"] = True
            self.sessions[sid] = state
        return self.sessions[sid]

    def _generation_state(self, sid):
        state = self._state(sid)
        if any(task[0] == sid for task in self.queue) or any(
            task["session_id"] == sid
            for task in (*self.active.values(), *self.agent_tasks.values())
        ):
            return "generating"
        if state.get("deleted"):
            return "not_started"
        session = self.ctx.streams.get_session(sid)
        cancelled = session is not None and (
            session.meta.get("input_failed") or session.meta.get("processing_cancelled")
        )
        if (
            state.get("restart_cancelled")
            or cancelled
            or state["phase"] in ("published", "failed", "cancelled")
        ):
            return "finished"
        if any(chunk["state"] in ("pending", "running") for chunk in state["chunks"]):
            return "generating"
        _, final = self._readiness(sid, [])
        if state["finalize_requested"] or (
            not final
            and self.setting("instant-summarize.enabled", True)
            and session is not None
        ):
            return "generating"
        return "finished" if state["chunks"] else "not_started"

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
            if getattr(event, "type", "") == "session.started":
                state.pop("restart_cancelled", None)
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
            if self._generation_state(sid) != "not_started":
                return False
            state["deleted"] = False
            state.pop("restart_cancelled", None)
            state["phase"] = "collecting"
            state["finalize_requested"] = True
            self._save(state)
            self.condition.notify_all()
        return True

    def finalize(self, session_id=""):
        sid = self._sid(session_id)
        with self.condition:
            state = self._state(sid)
            if (
                state.get("restart_cancelled")
                or state["phase"] in ("published", "failed", "cancelled")
                or state.get("deleted")
            ):
                return False
            state["finalize_requested"] = True
            self.condition.notify_all()
        return True

    def stop(self, session_id=""):
        sid = self._sid(session_id)
        with self.condition:
            state = self._state(sid)
            if self._generation_state(sid) != "generating":
                return False
            state["restart_cancelled"] = True
            state["finalize_requested"] = False
            self.queue = deque(task for task in self.queue if task[0] != sid)
            for chunk in state["chunks"]:
                if chunk["state"] in ("pending", "running"):
                    chunk["state"] = "cancelled"
            running = [
                key
                for key, task in self.agent_tasks.items()
                if task["session_id"] == sid
            ]
            self.condition.notify_all()
        for key in running:
            self.agent.cancel(key)
        with self.condition:
            self.condition.wait_for(
                lambda: (
                    not any(task["session_id"] == sid for task in self.active.values())
                    and not any(
                        task["session_id"] == sid for task in self.agent_tasks.values()
                    )
                )
            )
            state["phase"] = "cancelled"
            self._save(state)
        self.ctx.ui.refresh("notes.stopped")
        return True

    def delete(self, session_id=""):
        sid = self._sid(session_id)
        with self.condition:
            state = self._state(sid)
            if self._generation_state(sid) != "finished":
                return False
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
                phase="deleted",
                error="",
                errors=[],
                review_status="pending",
                deleted=True,
                finalize_requested=False,
                last_agent_tasks=[],
            )
            self._save(state)
        self.ctx.ui.refresh("notes.deleted")
        return True

    def status(self, session_id=""):
        sid = self._sid(session_id)
        with self.condition:
            state = self._state(sid)
            session = self.ctx.streams.get_session(sid)
            duration = (
                float(session.meta.get("total_media_seconds") or 0) if session else 0
            )
            chunk_enabled = bool(self.setting("instant-summarize.enabled", True))
            expected_chunks = (
                math.ceil(
                    duration / max(1, float(self.setting("notes.chunk_seconds", 600)))
                )
                if duration > 0 and chunk_enabled
                else (0 if not chunk_enabled else None)
            )
            return {
                "session_id": sid,
                "generation_state": self._generation_state(sid),
                "phase": state["phase"],
                "pending_chunks": sum(c["state"] == "pending" for c in state["chunks"]),
                "running_chunks": sum(c["state"] == "running" for c in state["chunks"]),
                "published_end": state["published_end"],
                "review_status": state["review_status"],
                "completed_chunks": sum(
                    c["state"] in ("done", "empty") for c in state["chunks"]
                ),
                "total_chunks": len(state["chunks"]),
                "expected_chunks": expected_chunks,
                "chunk_enabled": chunk_enabled,
                "tasks": sorted(
                    [
                        *state.get("last_agent_tasks", []),
                        *(
                            dict(task)
                            for task in self.agent_tasks.values()
                            if sid == task["session_id"]
                        ),
                    ],
                    key=lambda task: task["started"],
                ),
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
                        "state": "running",
                        "cancel": {
                            "action": "notes-pipeline.stop",
                            "payload": {"session_id": sid},
                        },
                        "label": f"笔记 {sid}",
                        "detail": (
                            f"分段整理；片段 {task['chunk']}；{elapsed} 秒"
                            if phase == "chunking"
                            else f"整课写作；{elapsed} 秒"
                        ),
                    }
                )
            tasks.extend(
                {
                    "id": f"notes:{sid}:{kind}:{index}",
                    "session_id": sid,
                    "label": f"笔记 {sid}",
                    "detail": f"排队；{kind}；片段 {index}",
                    "phase": kind,
                    "state": "pending",
                    "cancel": {
                        "action": "notes-pipeline.stop",
                        "payload": {"session_id": sid},
                    },
                    "chunk": index,
                }
                for sid, kind, index in self.queue
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
        if (
            state.get("deleted")
            or state.get("restart_cancelled")
            or state["phase"] in ("published", "failed", "cancelled")
        ):
            return
        session = self.ctx.streams.get_session(sid)
        if session is not None and (
            session.meta.get("input_failed") or session.meta.get("processing_cancelled")
        ):
            return
        records = collect_material(self.ctx, sid)
        before, final = self._readiness(sid, records)
        if final and not records:
            state["finalize_requested"] = False
            self._save(state)
            return
        seconds = max(1, float(self.setting("notes.chunk_seconds", 600)))
        end = max(
            (
                float(record.get("end") or record.get("start") or 0)
                for record in records
            ),
            default=0,
        )
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
                and not any(t["session_id"] == sid for t in self.active.values())
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
                    if state.get("deleted") or state.get("restart_cancelled"):
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
                if self._state(sid).get("deleted") or self._state(sid).get(
                    "restart_cancelled"
                ):
                    return
                if self.closed:
                    if kind == "chunk":
                        self._state(sid)["chunks"][index]["state"] = "pending"
                    self._save(self._state(sid))
                    return
                state = self._state(sid)
                state["errors"].append({"chunk": index, "error": str(exc)})
                if kind == "chunk":
                    state["chunks"][index].update(state="failed", error=str(exc))
                else:
                    state["error"] = str(exc)
                    state["phase"] = "failed"
                self._save(state)
        finally:
            with self.condition:
                self.active.pop(token, None)
                self.condition.notify_all()

    @staticmethod
    def _execution_agent(key, role, label, model=""):
        return {
            "id": key,
            "role": role,
            "label": label,
            "status": "running",
            "model": model,
            "thinking": "",
            "context_tokens": 0,
            "context_window": 0,
            "llm_seconds": 0,
            "tool_seconds": 0,
            "output_tokens": 0,
            "tokens_per_second": 0,
            "blocks": [],
        }

    @staticmethod
    def _merge_execution_block(agent, block):
        for index, existing in enumerate(agent["blocks"]):
            if existing["id"] == block["id"]:
                agent["blocks"][index] = dict(block)
                return
        agent["blocks"].append(dict(block))

    def _agent_event(self, event):
        with self.condition:
            task = self.agent_tasks.get(str(event.get("session_id", "")))
            if task is None:
                return
            task["agent_step"] = int(event.get("step", task.get("agent_step", 0)))
            agents = task["agents"]
            reviewer = event.get("reviewer_id")
            key = reviewer or "main"
            name = event.get("event") if reviewer else event.type
            if key not in agents:
                agents[key] = self._execution_agent(
                    key,
                    event.get("role") if reviewer else "main",
                    {
                        "note_review": "笔记审阅",
                    }.get(event.get("role"), task.get("label", "主写作")),
                )
            agent = agents[key]
            now = time.monotonic()
            if name == "agent.execution":
                agent.update(event.get("metrics") or {})
                if event.get("block") is not None:
                    self._merge_execution_block(agent, event.get("block"))
            elif name == "agent.file":
                for child in agents.values():
                    if child["status"] in ("passed", "needs_revision"):
                        child["status"] = "stale"
            elif name == "finished":
                report = json.loads(event.get("content"))
                agent["status"] = (
                    "passed"
                    if report["overall_correctness"] == "correct"
                    else "needs_revision"
                )
                self._merge_execution_block(
                    agent,
                    {
                        "id": f"{key}:review",
                        "kind": "review",
                        "text": event.get("content"),
                        "report": report,
                        "status": "completed",
                    },
                )
            elif name == "failed":
                agent["status"] = "failed"
                self._merge_execution_block(
                    agent,
                    {
                        "id": f"{key}:review",
                        "kind": "review",
                        "text": str(event.get("error", "审阅失败")),
                        "status": "failed",
                    },
                )
            refresh = now - task.get("last_refresh", 0) >= 1 or name in (
                "started",
                "finished",
                "failed",
                "agent.file",
            )
            if refresh:
                task["last_refresh"] = now
                self.ctx.ui.refresh("notes.activity")

    def _check_running(self, sid):
        if (
            self.closed
            or self._state(sid).get("deleted")
            or self._state(sid).get("restart_cancelled")
        ):
            raise RuntimeError("Note generation cancelled")

    @contextmanager
    def _agent_slot(self, sid):
        with self.condition:
            self.condition.wait_for(
                lambda: (
                    self.closed
                    or self._state(sid).get("deleted")
                    or self._state(sid).get("restart_cancelled")
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
            with self.condition:
                self._check_running(sid)
                self.agent_tasks[session_id] = {
                    "id": f"notes-agent:{session_id}",
                    "session_id": sid,
                    "label": session.name,
                    "detail": "执行中",
                    "agent_step": 0,
                    "started": time.monotonic(),
                    "agents": {
                        "main": self._execution_agent(
                            "main",
                            "main"
                            if session.meta.get("role") == "writer"
                            else f"chunk:{session_id}",
                            session.name,
                            session.model,
                        )
                    },
                }

            def on_event(event):
                if event.type == "agent.file":
                    with self.condition:
                        self._check_running(sid)
                        if session.meta.get("role") == "writer":
                            self._publish_lecture(sid, session.workspace, draft=True)
                            self.ctx.ui.refresh("notes.preview")

            try:
                result = self.agent.run(
                    session_id,
                    self._lecture_context(sid) + "\n" + instruction,
                    on_event=on_event,
                    write_guard=lambda path: publication_lock(
                        self.ctx.data_dir / "sessions" / sid / "notes"
                    ),
                )
                with self.condition:
                    self._check_running(sid)
                    task = self.agent_tasks[session_id]
                    task["agents"]["main"]["status"] = (
                        "failed" if result.error else "completed"
                    )
                return result
            finally:
                with self.condition:
                    task = self.agent_tasks.pop(session_id, None)
                    if task is not None:
                        if task["agents"]["main"]["status"] == "running":
                            task["agents"]["main"]["status"] = "failed"
                        self._state(sid).setdefault("last_agent_tasks", []).append(task)
                    self.condition.notify_all()

    def _run_agent(self, sid, name, workspace, instruction):
        session = self.agent.create_session(
            name,
            workspace=workspace,
            system_prompt=self.setting("instant-summarize.system_prompt", "") or SYSTEM,
            role="note_chunk",
            tools=["read", "grep", "glob", "edit", "write"],
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
        (root / SOURCE_FILENAME).write_text(render(records), encoding="utf-8")
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
        files = {
            name: root / name
            for name in (
                "source.md",
                "context.md",
                "visual_history.md",
                SOURCE_FILENAME,
            )
        }
        for name, header in NOTE_HEADERS.items():
            files[name] = root / name
            files[name].write_text(header, encoding="utf-8")
        workspace = FileWorkspace(
            files,
            readonly=("source.md", "context.md", "visual_history.md", SOURCE_FILENAME),
        )
        self._run_agent(sid, f"{sid} 片段 {index}", workspace, CHUNK)
        self._validate_outputs(root)
        with self.condition:
            self._check_running(sid)
            self._state(sid)["chunks"][index]["state"] = "done"
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
                latest = (
                    destination.read_text(encoding="utf-8")
                    if destination.exists()
                    else ""
                )
                seen = set()
                blocks = []
                for block in parse_blocks(latest):
                    if block.id not in seen:
                        seen.add(block.id)
                        blocks.append(latest[block.start : block.end])
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
            if not files[name].exists():
                files[name].write_text(header, encoding="utf-8")
        workspace = FileWorkspace(files, readonly=readonly)
        review_enabled = bool(self.setting("summarize.review_enabled", True))
        review = {
            "prompt": REVIEW,
            "system": self.setting("summarize.reviewer_prompt", "") or REVIEW_SYSTEM,
            "maxRounds": int(self.setting("summarize.review_max_rounds", 0)),
        }
        writer = self.agent.create_session(
            f"{sid} 整课写作",
            workspace=workspace,
            system_prompt=self.setting("summarize.system_prompt", "") or SYSTEM,
            role="note_writer",
            tools=["read", "grep", "glob", "edit", "write"],
            meta={
                "internal": True,
                "role": "writer",
                "lecture_session": sid,
                **({"review": review} if review_enabled else {}),
            },
        )
        review_status = "passed" if review_enabled else "disabled"
        try:
            instruction = WRITER
            if review_enabled:
                instruction += "\n保存文稿后调用 review_notes．核对审阅依据后修订，每次修改后必须再次调用，直到审阅通过．"
            result = self._agent_run(sid, writer.id, instruction)
            if result.error:
                raise RuntimeError(result.error)
            self._validate_outputs(directory)
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

    def teardown(self):
        with self.condition:
            self.closed = True
            self.queue.clear()
            for session_id in tuple(self.agent_tasks):
                self.agent.cancel(session_id)
            self.condition.notify_all()
        self.coordinator.join()
        self.pool.shutdown(wait=True, cancel_futures=True)
