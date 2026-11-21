from __future__ import annotations

import json
import threading
from functools import partial
from pathlib import Path

from core.agent_extensions import (
    AgentCompletionHook,
    AgentExtension,
    AgentExtensionTool,
)
from core.events import Event
from core.plugin import Plugin, PluginContext
from core.settings_spec import SettingType, SettingsSpec

from .checker import check_file
from .prompts import CHUNK, COMMON, REVIEW, WRITER

TOOL_NAME = "note_check_stc"
ENABLED_KEY = "note_check_stc.enabled"
AVOID_KEY = "note_check_stc.avoid"


def check_note(context, *, settings):
    name = context["parameters"].get("path")
    workspace = context["workspace"]
    if not isinstance(name, str) or name not in workspace:
        report = {"error": "文件未获授权．path 必须是工作区逻辑文件名．"}
    else:
        try:
            report = check_file(
                Path(workspace[name]), name=name, avoid=settings.get(AVOID_KEY)
            )
        except (OSError, UnicodeError):
            report = {
                "file": name,
                "error": "无法读取 UTF-8 文件．检查文件是否存在及其编码．",
            }
        except ValueError as exc:
            report = {"file": name, "error": str(exc)}
    return {
        "content": [{"type": "text", "text": json.dumps(report, ensure_ascii=False)}],
        "details": report,
        "isError": "error" in report,
    }


def require_check(context, *, targets):
    for call in context["tool_calls"]:
        if call.get("name") != TOOL_NAME:
            continue
        result = call.get("result")
        if not isinstance(result, dict) or result.get("isError"):
            continue
        report = result.get("details")
        if (
            isinstance(report, dict)
            and "error" not in report
            and isinstance(report.get("issues"), list)
            and report.get("file") in targets
            and report.get("file") in context["workspace"]
        ):
            return None
    return {
        "continue": "本轮尚未成功调用 note_check_stc．使用工作区逻辑文件名检查受审笔记后再返回报告．"
    }


class NoteCheckSTCPlugin(Plugin):
    id = "note_check_stc"
    requires = ("settings", "agent", "notes")

    def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.agent = ctx.require("agent")
        self.settings = ctx.require("settings")
        self.targets = tuple(
            name for name, _ in ctx.require("notes").note_files().values()
        )
        self._lock = threading.RLock()
        self._enabled = False
        self.settings.register(
            SettingsSpec(
                key=ENABLED_KEY,
                label="启用 STC 笔记检查",
                type=SettingType.BOOL,
                default=True,
                group="笔记",
                section="审阅",
                description="关闭后，新建或重开的笔记会话不再附加 STC 规则和检查工具．",
                order=120,
            ),
            plugin_id=self.id,
        )
        self.settings.register(
            SettingsSpec(
                key=AVOID_KEY,
                label="STC avoid.json 词表",
                type=SettingType.JSON,
                default=json.loads(
                    Path(__file__).with_name("avoid.json").read_text(encoding="utf-8")
                ),
                group="笔记",
                section="审阅",
                description="JSON 数组，每项包含 avoid、type、why、use．保存后下次检查生效．",
                order=121,
            ),
            plugin_id=self.id,
        )
        ctx.events.on("settings.changed", self._on_settings_changed)
        self._sync_enabled()

    def _on_settings_changed(self, event: Event) -> None:
        if event.get("key") == ENABLED_KEY:
            self._sync_enabled()

    def _sync_enabled(self) -> None:
        with self._lock:
            enabled = self.settings.get(ENABLED_KEY)
            if enabled == self._enabled:
                return
            if enabled:
                self.agent.register_extension(
                    self.id,
                    AgentExtension(
                        id="rules",
                        roles=("note_chunk", "note_writer", "note_review"),
                        prompt=COMMON,
                    ),
                )
                for role, prompt in (("note_chunk", CHUNK), ("note_writer", WRITER)):
                    self.agent.register_extension(
                        self.id, AgentExtension(id=role, roles=(role,), prompt=prompt)
                    )
                self.agent.register_extension(
                    self.id,
                    AgentExtension(
                        id="review",
                        roles=("note_review",),
                        prompt=REVIEW,
                        tools=(
                            AgentExtensionTool(
                                name=TOOL_NAME,
                                label="STC 笔记检查",
                                description="只读检查一个授权 Markdown 笔记文件，返回含位置、规则和建议的 JSON 诊断．",
                                parameters={
                                    "type": "object",
                                    "properties": {
                                        "path": {"type": "string", "minLength": 1}
                                    },
                                    "required": ["path"],
                                    "additionalProperties": False,
                                },
                                handler=partial(check_note, settings=self.settings),
                            ),
                        ),
                        completion_hooks=(
                            AgentCompletionHook(
                                id="check",
                                handler=partial(require_check, targets=self.targets),
                            ),
                        ),
                    ),
                )
            else:
                self.agent.unregister_extensions(self.id)
            self._enabled = enabled

    def teardown(self) -> None:
        with self._lock:
            self.agent.unregister_extensions(self.id)
            self._enabled = False
