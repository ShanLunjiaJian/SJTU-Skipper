"""Display incremental notes from the shared pipeline."""

from core.plugin import Plugin
from core.settings_spec import SettingsSpec
from plugins.notes_pipeline.prompts import SYSTEM


class InstantSummarizeService:
    def __init__(self, pipeline):
        self.pipeline = pipeline

    def draft_paths(self, session_id=""):
        return self.pipeline.draft_paths(session_id)

    def note_files(self):
        return self.pipeline.note_files("instant")

    def running_tasks(self):
        return self.pipeline.running_tasks()


class InstantSummarizePlugin(Plugin):
    id = "instant-summarize"
    requires = ("settings", "notes-pipeline", "reader")
    provides = ("instant-summarize",)

    def setup(self, ctx):
        self.ctx = ctx
        self.settings = ctx.require("settings")
        self.pipeline = ctx.require("notes-pipeline")
        for key, default, kind, label in (
            ("system_prompt", SYSTEM, "string", "系统提示词"),
            ("enabled", True, "bool", "分段笔记"),
        ):
            self.settings.register(
                SettingsSpec(
                    key=f"instant-summarize.{key}",
                    default=default,
                    type=kind,
                    label=label,
                    group="Instant Summarize",
                )
            )
        ctx.register_service(self.id, InstantSummarizeService(self.pipeline))
        if not ctx.ui.has_tab("note"):
            ctx.ui.add_tab("right", "note", "笔记", order=20, plugin_id=self.id)
        ctx.ui.add_toolbox_item(
            "note",
            "instant-summarize.notes",
            "分段笔记",
            order=11,
            render=self._render,
            plugin_id=self.id,
        )
        ctx.ui.register_action(
            "instant-summarize.toggle", self._toggle, plugin_id=self.id
        )
        ctx.ui.register_action("instant-summarize.read", self._read, plugin_id=self.id)

    def _render(self):
        status = self.pipeline.status()
        return [
            {
                "type": "switch",
                "label": "分段笔记",
                "checked": bool(self.settings.get("instant-summarize.enabled", True)),
                "action": "instant-summarize.toggle",
            },
            {
                "type": "text",
                "text": f"已发布至 {status['published_end']:.0f} 秒；缺口 {len(status['errors'])} 项",
            },
            {
                "type": "buttons",
                "buttons": [
                    {
                        "label": "打开分段笔记",
                        "action": "instant-summarize.read",
                    }
                ],
            },
        ]

    def _toggle(self, payload):
        enabled = payload.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.lower() in ("true", "1", "yes", "on")
        self.settings.set("instant-summarize.enabled", bool(enabled))
        self.ctx.ui.refresh("notes.toggle")
        return {"ok": True, "refresh": True}

    def _read(self, payload):
        sid = self.ctx.streams.notes_session_id
        docs = [
            {
                "key": key,
                "label": label,
                "path": str(self.ctx.data_dir / "sessions" / sid / "notes" / name),
            }
            for key, (name, label) in self.pipeline.note_files("instant").items()
        ]
        page = self.ctx.require("reader").show(
            title="分段笔记",
            docs=docs,
            active=str(payload.get("doc", "knowledge")),
            page_key=f"{sid}:instant-summarize",
            owner=self.id,
        )
        return {"ok": True, "open_page": page}

    def teardown(self):
        self.ctx.ui.unregister_action("instant-summarize.toggle")
        self.ctx.ui.unregister_action("instant-summarize.read")
        self.ctx.ui.remove_toolbox_item("instant-summarize.notes")
