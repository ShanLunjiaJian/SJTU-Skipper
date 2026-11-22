"""Learning-state memory: SQLite keyword entries exposed as agent tools."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

from core.agent_extensions import AgentExtension, AgentExtensionTool
from core.plugin import Plugin, PluginContext
from core.ui import BlockType, FormInputType, Side

from .store import MAX_DESCRIPTION, MAX_KEYWORDS, LearningStore

LOG = logging.getLogger("lecture_agent.plugin.learning")
TAB_ID = "learning"
ITEM_ID = "learning.panel"
READ_ONLY_ROLES = {"note_chunk", "note_writer", "note_review", "teaching_review"}
def _values(payload): 
    nested = payload.get("values")
    return nested if isinstance(nested, Mapping) else payload


def _text(payload, *names):
    values = _values(payload)
    for name in names:
        if values.get(name) is not None:
            return str(values.get(name)).strip()
    return ""


def _field(name, label, value="", *, type=FormInputType.STRING, input=FormInputType.TEXT, **options):
    return {"name": name, "key": name, "label": label, "type": type, "input": input, "value": value, **options}


class LearningService:
    __slots__ = ("_plugin",)

    def __init__(self, plugin: "LearningPlugin") -> None:
        self._plugin = plugin

    def search(self, keywords, *, limit=20):
        return self._plugin.store.search(keywords, limit=limit)

    def upsert(self, keywords, description, *, mode="insert"):
        return self._plugin.store.upsert(keywords, description, mode=mode)


class LearningPlugin(Plugin):
    id = "learning"
    requires = ("settings", "agent")
    provides = ("learning",)
    action_namespace = "learning"
    optional_requires = ("conversation",)

    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.agent: Any = None
        self.store: LearningStore | None = None

    def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.agent = ctx.require("agent")
        self.settings = ctx.require("settings")
        self.store = LearningStore(ctx.data_dir / "learning" / "learning.db")
        self._query = ""
        self._admin_sessions: dict[str, str] = {}
        ctx.register_service("learning", LearningService(self))
        for action, handler in (
            ("learning.remove", self._remove),
            ("learning.update_desc", self._update_desc),
            ("learning.search", self._search_action),
            ("learning.admin_open", self._admin_open),
            ("learning.close_admin", self._close_admin),
        ):
            ctx.ui.register_action(action, handler, plugin_id=self.id)
        conversation_service = ctx.service("conversation")
        if conversation_service is not None:
            conversation_service.register_provider("learning-admin", self._admin_send, self._admin_extra)
        self.settings.register_section("learning", "学情记忆", self._render_settings)
        self._register()

    def teardown(self) -> None:
        ctx = self.ctx
        if ctx is None:
            return
        try:
            self.agent.unregister_extensions(self.id)
        except Exception:  # noqa: BLE001
            pass
        for action in ("remove", "update_desc", "search", "admin_open", "close_admin"):
            try:
                ctx.ui.unregister_action(f"learning.{action}")
            except Exception:  # noqa: BLE001
                pass
        try:
            self.settings.unregister_section("learning")
        except Exception:  # noqa: BLE001
            pass
        try:
            service = ctx.service("conversation")
            if service is not None:
                service.unregister_provider("learning-admin")
        except Exception:  # noqa: BLE001
            pass
        for session_id in list(self._admin_sessions.values()):
            try:
                self.agent.close(session_id)
            except Exception:  # noqa: BLE001
                pass
        self._admin_sessions.clear()
        self.ctx = None

    def _register(self) -> None:
        tools = (
            AgentExtensionTool(
                name="learning_search",
                label="查询学情记忆",
                description=(
                    "查询学生的学情记忆。学情 = 某个具体数学知识点上学生会不会/熟不熟，"
                    "不是课程知识本身，也不是出勤记录。\n"
                    "keywords: 1–5 个正式数学名称或常见别名，OR 近似匹配；用不同叫法一起搜。\n"
                    "返回 entries: [{id, keywords, description, updated_at}]。\n"
                    "entries 为空表示「不知道学生会不会」，不等于「学生不会」；"
                    "不要因此跳过内容或判学生没学过。讲解/出题/写笔记/写教案前调用。\n"
                    "如果 search 结果存在则应该相信；如果没有迹象表明 search 结果过时"
                    "（迹象包括 search 结果说学生没学过，但学生说自己会之类的），就不要问学生。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "required": ["keywords"],
                    "additionalProperties": False,
                },
                handler=self._search_tool,
            ),
            AgentExtensionTool(
                name="learning_upsert",
                label="写入学情记忆",
                description=(
                    "记录/更新一个具体数学知识点的学生掌握状态。\n"
                    "只在学生明确说出「学过/没学过/不熟/只会计算不会证明」一类话时调用；"
                    "不要从答错、要求举例、听不懂某一步推断。\n"
                    "keywords: 该知识点或领域的 1–5 个正式名称/别名，不要用句子。\n"
                    "description: 「知识点/领域：状态」，例如「伽罗瓦理论：没学过」「初等数论：学过」"
                    "「有限域：学过但证明不熟」；不要写「上节课没听」这类课堂事件或时效性强的内容。\n"
                    "mode：\n"
                    "- insert：插入一条新学情；\n"
                    "- replace：已有条目命中任一 keyword 时，用本次 keywords/description 覆盖它。\n"
                    "没有把握是否同一知识点时先用 learning_search；避免重复条目。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "description": {"type": "string", "minLength": 1},
                        "mode": {"type": "string", "enum": ["insert", "replace"]},
                    },
                    "required": ["keywords", "description"],
                    "additionalProperties": False,
                },
                handler=self._upsert_tool,
            ),
        )
        try:
            self.agent.register_extension(
                self.id,
                AgentExtension(id="learning-memory", roles=("*",), tools=tools),
            )
        except Exception:  # noqa: BLE001
            LOG.warning("learning extension registration failed", exc_info=True)

    def _search_tool(self, context: Mapping[str, Any]):
        params = context.get("parameters") or {}
        rows = self.store.search(params.get("keywords") or [], limit=int(params.get("limit") or 20))
        return {"content": [{"type": "text", "text": __import__("json").dumps(rows, ensure_ascii=False)}], "details": {"rows": rows}}

    def _upsert_tool(self, context: Mapping[str, Any]):
        role = str(context.get("role") or "")
        if role in READ_ONLY_ROLES:
            return {
                "content": [{"type": "text", "text": f"当前角色 {role} 只能读取学情记忆，不能写入。"}],
                "isError": True,
                "details": {},
            }
        params = context.get("parameters") or {}
        description = str(params.get("description") or "")
        result = self.store.upsert(
            params.get("keywords") or [], str(params.get("description") or ""), mode=str(params.get("mode") or "insert")
        )
        self.refresh()
        return {
            "content": [{"type": "text", "text": str(result.get("message") or "已记录")}],
            "isError": not bool(result.get("ok")),
            "details": result,
        }

    def refresh(self):
        if self.ctx is not None:
            try:
                self.ctx.ui.refresh("learning")
            except Exception:  # noqa: BLE001
                pass

    def _remove(self, payload):
        ok = self.store.remove(int(_text(payload, "id") or 0))
        self.refresh()
        return {"ok": ok, "message": "已删除" if ok else "没有找到条目", "refresh": True}

    def _search_action(self, payload):
        self._query = _text(payload, "query")
        self.refresh()
        return {"ok": True, "refresh": True}

    def _update_desc(self, payload):
        values = _values(payload)
        entry_id = int(values.get("id") or 0)
        text = str(values.get("description") or "").strip()
        if entry_id <= 0 or not text:
            return {"ok": False, "message": "描述不能为空"}
        if not self.store.update_description(entry_id, text):
            return {"ok": False, "message": "没有找到条目"}
        self.refresh()
        return {"ok": True, "message": "描述已更新", "refresh": True}

    def _admin_open(self, payload):
        service = self.ctx.service("conversation") if self.ctx is not None else None
        if service is None:
            return {"ok": False, "message": "conversation 插件未启用"}
        conversation_id = service.create(
            "learning-admin",
            "修改学情记忆",
            {"session_id": "", "session_title": "学情记忆"},
            transient=True,
        )
        return {"ok": True, "open_page": f"conversation:{conversation_id}", "refresh": True}

    def _admin_extra(self, conversation_id: str, context: Mapping[str, Any]):
        return [{
            "type": BlockType.BUTTONS,
            "buttons": [{
                "label": "结束修改对话",
                "action": "learning.close_admin",
                "value": {"conversation_id": conversation_id},
                "class": "btn btn-sm",
            }],
        }]

    def _close_admin(self, payload):
        conversation_id = str(_values(payload).get("conversation_id") or "")
        session_id = self._admin_sessions.pop(conversation_id, "")
        if session_id:
            try:
                self.agent.close(session_id)
            except Exception:  # noqa: BLE001
                pass
        service = self.ctx.service("conversation")
        if service is not None:
            service.delete(conversation_id)
        return {"ok": True, "refresh": True, "message": "已结束修改对话"}

    def _admin_send(self, conversation_id: str, text: str, context: Mapping[str, Any]) -> None:
        service = self.ctx.service("conversation")
        if service is None:
            return
        session_id = self._admin_sessions.get(conversation_id, "")
        if not session_id:
            try:
                session = self.agent.create_session(
                    name="学情修改对话",
                    role="default",
                    system_prompt=(
                        "你是学情记忆管理员。用户会描述要修改的学情；先调用 learning_search "
                        "找到条目，再用 learning_upsert 修改 description（keywords 由你按 tool "
                        "描述维护，不要凭空改无关条目）。每次只做用户要求的修改，完成后简短说明。"
                    ),
                    tools=[],
                )
                session_id = session.id
                self._admin_sessions[conversation_id] = session_id
            except Exception as exc:  # noqa: BLE001
                service.append(conversation_id, "system", f"无法创建对话：{exc}")
                return
        try:
            result = self.agent.run(session_id, text)
            service.append(conversation_id, "assistant", result.final_message or "（没有回答）")
        except Exception as exc:  # noqa: BLE001
            service.append(conversation_id, "system", f"修改失败：{exc}")
        self.refresh()

    def _render_settings(self):
        store = self.store
        if store is None:
            return [{"type": BlockType.TEXT, "text": "学情服务未就绪"}]
        entries = [entry for entry in store.items() if not self._query or self._query.lower() in (entry["description"] + " ".join(entry["keywords"])).lower()]
        blocks: list[dict[str, Any]] = [
            {"type": BlockType.MARKDOWN, "text": f"### 学情记忆\n\n共 **{len(entries)}** 条。agent 自动写入；用户只能搜索/删除/改描述，keywords 由 agent 维护。"},
            {"type": BlockType.FORM, "fields": [{"name": "query", "key": "query", "label": "搜索（描述或关键词）", "type": FormInputType.STRING, "input": FormInputType.TEXT, "value": self._query}], "submit": {"label": "搜索", "action": "learning.search"}},
        ]
        for entry in entries:
            blocks.append({"type": BlockType.MARKDOWN, "text": f"**#{entry['id']}** 关键词：{'、'.join(entry['keywords'])}"})
            blocks.append({
                "type": BlockType.FORM,
                "fields": [
                    {"name": "id", "key": "id", "label": "", "type": FormInputType.STRING, "input": FormInputType.TEXT, "value": entry["id"], "hidden": True},
                    {"name": "description", "key": "description", "label": "描述", "type": FormInputType.STRING, "input": FormInputType.TEXT, "value": entry["description"]},
                ],
                "submit": {"label": "保存描述", "action": "learning.update_desc"},
            })
            blocks.append({"type": BlockType.BUTTONS, "buttons": [{"label": "删除", "action": "learning.remove", "value": {"id": entry["id"]}, "class": "btn btn-sm btn-danger"}]})
        if self.ctx.service("conversation") is not None:
            blocks.append({"type": BlockType.BUTTONS, "buttons": [{"label": "用对话修改学情", "action": "learning.admin_open", "class": "btn-solid theme-primary"}]})
        return blocks


__all__ = ["LearningPlugin", "LearningService"]
