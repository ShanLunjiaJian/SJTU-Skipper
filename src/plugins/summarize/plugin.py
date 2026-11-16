"""Control reviewed lecture notes and recording gap collection."""

import json
import threading
import time
from core.plugin import Plugin
from core.settings_spec import SettingsSpec
from plugins.notes_pipeline.prompts import SYSTEM, REVIEW_SYSTEM


def note_status_blocks(state):
    phase = {
        "collecting": "收集来源",
        "chunking": "整理片段",
        "writing": "整课写作",
        "reviewing": "审阅",
        "revising": "修改",
        "published": "已发布",
        "failed": "失败",
        "resetting": "清理笔记",
        "deleted": "已删除",
    }[state["phase"]]
    blocks = [
        {"type": "text", "text": f"笔记：{phase}"},
        {
            "type": "text",
            "text": f"片段 {state['completed_chunks']}/{state['total_chunks']}；等待 {state['pending_chunks']}；运行 {state['running_chunks']}",
        },
    ]
    for task in state["tasks"]:
        elapsed = max(0, int(time.monotonic() - task["started"]))
        blocks.append(
            {
                "type": "text",
                "text": f"{task['label']}：步骤 {task['agent_step']}；{elapsed} 秒",
            }
        )
    if state["error"]:
        blocks.append(
            {
                "type": "text",
                "text": f"笔记生成失败：{state['error']}．可重新生成笔记．",
            }
        )
    return blocks


class SummarizeService:
    def __init__(self, plugin):
        self.plugin = plugin

    @property
    def busy(self):
        state = self.plugin.pipeline.status()
        return bool(
            state["pending_chunks"]
            or state["running_chunks"]
            or state["phase"] in ("writing", "reviewing", "revising")
        )

    def start(self, session_id=""):
        return self.plugin.pipeline.start(session_id)

    def source_path(self, session_id=""):
        return self.plugin.pipeline.source_path(session_id)

    def note_files(self):
        return self.plugin.pipeline.note_files("final")

    def running_tasks(self):
        return self.plugin.pipeline.running_tasks()


class SummarizePlugin(Plugin):
    id = "summarize"
    requires = ("settings", "notes-pipeline", "reader")
    optional_requires = ("input",)
    provides = ("summarize",)
    ON_STOP_LABELS = (
        ("direct", "直接生成讲义"),
        ("fill", "先补全再生成"),
        ("skip", "不自动生成讲义"),
    )

    def setup(self, ctx):
        self.ctx = ctx
        self.settings = ctx.require("settings")
        self.pipeline = ctx.require("notes-pipeline")
        self.stop_event = threading.Event()
        self.notices = {}
        self.fill_threads = []
        for key, default, kind in (
            ("system_prompt", SYSTEM, "string"),
            ("reviewer_prompt", REVIEW_SYSTEM, "string"),
            ("model", "", "string"),
            ("max_tokens", 32000, "int"),
            ("max_tool_output", 200000, "int"),
            ("max_history_chars", 0, "int"),
            ("review_enabled", True, "bool"),
            ("review_model", "", "string"),
            ("review_max_steps", 0, "int"),
            ("review_max_tokens", 32000, "int"),
            ("review_max_rounds", 0, "int"),
            ("fill_wait_minutes", 20, "int"),
            ("fill_max_attempts", 6, "int"),
        ):
            self.settings.register(
                SettingsSpec(
                    key=f"summarize.{key}",
                    default=default,
                    type=kind,
                    label=key,
                    group="Summarize",
                )
            )
        self.settings.register(
            SettingsSpec(
                key="summarize.on_stop",
                default="direct",
                type="select",
                label="课后处理",
                options=list(self.ON_STOP_LABELS),
                group="Summarize",
            )
        )
        ctx.register_service(self.id, SummarizeService(self))
        if not ctx.ui.has_tab("note"):
            ctx.ui.add_tab("right", "note", "笔记", order=20, plugin_id=self.id)
        ctx.ui.add_toolbox_item(
            "note",
            "summarize.notes",
            "课堂讲义",
            order=10,
            render=self._render,
            plugin_id=self.id,
        )
        for action, handler in (
            ("read", self._read),
            ("set_mode", self._set_mode),
            ("fill_again", self._fill_again),
            ("delete", self._delete),
            ("regenerate", self._regenerate),
        ):
            ctx.ui.register_action(f"summarize.{action}", handler, plugin_id=self.id)
        ctx.events.on("session.stopped", self._stopped, plugin_id=self.id)

    def _render(self):
        sid = self.ctx.streams.notes_session_id
        state = self.pipeline.status(sid)
        blocks = note_status_blocks(state)
        blocks.extend(
            [
                {
                    "type": "buttons",
                    "buttons": [
                        {
                            "label": "打开课堂讲义",
                            "action": "summarize.read",
                        }
                    ],
                },
                {
                    "type": "buttons",
                    "buttons": [
                        {"label": "生成笔记", "action": "summarize.regenerate"},
                        {"label": "补录缺失内容", "action": "summarize.fill_again"},
                        {"label": "删除笔记", "action": "summarize.delete"},
                    ],
                },
                {
                    "type": "form",
                    "fields": [
                        {
                            "name": "mode",
                            "label": "课后处理",
                            "type": "select",
                            "options": [
                                {"value": key, "label": label}
                                for key, label in self.ON_STOP_LABELS
                            ],
                            "value": self.settings.get("summarize.on_stop", "direct"),
                        }
                    ],
                    "submit": {"label": "保存", "action": "summarize.set_mode"},
                },
            ]
        )
        if sid in self.notices:
            blocks.insert(1, {"type": "text", "text": self.notices[sid]})
        return blocks

    def _delete(self, payload):
        self.pipeline.reset(
            str(payload.get("session_id") or self.ctx.streams.notes_session_id)
        )
        return {"ok": True, "refresh": True, "message": "正在删除笔记"}

    def _regenerate(self, payload):
        self.pipeline.reset(
            str(payload.get("session_id") or self.ctx.streams.notes_session_id),
            regenerate=True,
        )
        return {"ok": True, "refresh": True, "message": "正在生成笔记"}

    def _read(self, payload):
        sid = self.ctx.streams.notes_session_id
        docs = [
            {
                "key": key,
                "label": label,
                "path": str(self.ctx.data_dir / "sessions" / sid / "notes" / name),
            }
            for key, (name, label) in self.pipeline.note_files("final").items()
        ]
        page = self.ctx.require("reader").show(
            title="课堂讲义",
            docs=docs,
            active=str(payload.get("doc", "knowledge")),
            page_key=f"{sid}:summarize",
            owner=self.id,
        )
        return {"ok": True, "open_page": page}

    def _set_mode(self, payload):
        mode = str(payload.get("mode", ""))
        if mode not in dict(self.ON_STOP_LABELS):
            return {"ok": False, "message": "未知的课后处理方式"}
        self.settings.set("summarize.on_stop", mode)
        return {"ok": True, "refresh": True}

    def _meta(self, sid):
        session = self.ctx.streams.get_session(sid)
        if session is not None:
            return session.meta
        path = self.ctx.data_dir / "sessions" / sid / "meta.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw.get("meta", raw)
        return {}

    def _stopped(self, event):
        sid = str(event.get("session_id", ""))
        if not sid or sid == "boot":
            return
        meta = self._meta(sid)
        if meta.get("fill_for"):
            self.pipeline.start(str(meta["fill_for"]))
            return
        mode = self.settings.get("summarize.on_stop", "direct")
        if mode == "direct":
            self.pipeline.start(sid)
        elif mode == "fill":
            self._request_fill(sid)

    def _fill_again(self, payload):
        self._request_fill(
            str(payload.get("session_id") or self.ctx.streams.notes_session_id)
        )
        return {"ok": True, "message": "正在查找可补录的内容", "refresh": True}

    def _request_fill(self, sid):
        thread = threading.Thread(
            target=self._fill, args=(sid,), name=f"notes-fill-{sid}", daemon=True
        )
        self.fill_threads.append(thread)
        thread.start()

    def _fill(self, sid):
        service = self.ctx.service("input")
        attempts = max(1, int(self.settings.get("summarize.fill_max_attempts", 6)))
        for attempt in range(attempts):
            if self.stop_event.is_set():
                return
            try:
                if service is not None and service.fill_gaps(sid):
                    return
            except Exception as exc:
                self.notices[sid] = f"补录查询失败：{exc}"
            if attempt + 1 < attempts and self.stop_event.wait(
                max(1, int(self.settings.get("summarize.fill_wait_minutes", 20))) * 60
            ):
                return
        self.notices[sid] = "未取得录播，讲义使用已有来源，并保留来源缺口标记"
        self.pipeline.start(sid)
        self.ctx.ui.refresh("notes.fill")

    def teardown(self):
        self.stop_event.set()
        for thread in self.fill_threads:
            thread.join()
        for action in ("read", "set_mode", "fill_again", "delete", "regenerate"):
            self.ctx.ui.unregister_action(f"summarize.{action}")
        self.ctx.ui.remove_toolbox_item("summarize.notes")
