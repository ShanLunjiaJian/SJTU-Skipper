"""Render LLM settings with shared table editors."""

from __future__ import annotations

import json
from copy import deepcopy
from html import escape
from typing import Any, Mapping

from core.ui import BlockType, FormInputType

PAGE_ID = "llm:configuration"

# Endpoint presets: protocol, auth mode and URL are decided here so the UI never
# asks the user to remember spellings.
PROVIDER_PRESETS: tuple[tuple[str, str], ...] = (
    ("custom", "OpenAI 兼容端点"),
    ("opencode-go", "OpenCode Go"),
)
PRESETS: dict[str, dict[str, str]] = {
    "opencode-go": {
        "base_url": "https://opencode.ai/inference/go/openai/v1",
        "api": "openai-completions",
        "auth": "oauth",
    },
    "custom": {"base_url": "", "api": "openai-completions", "auth": "apiKey"},
}
API_OPTIONS: tuple[str, ...] = (
    "openai-completions",
    "openai-responses",
    "openrouter",
    "anthropic-messages",
    "google-generative-ai",
    "ollama-chat",
)
AUTH_OPTIONS: tuple[str, ...] = ("apiKey", "oauth", "none")
AUTH_LABELS: dict[str, str] = {
    "apiKey": "API Key",
    "oauth": "浏览器授权",
    "none": "无需鉴权",
}
INPUT_FORMATS: tuple[str, ...] = ("text", "image")
THINKING_LEVELS: tuple[str, ...] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "auto",
    "max",
)
ROLE_TITLES: dict[str, str] = {
    "default": "默认",
    "ask_instant": "快速问答",
    "ask_research": "研究问答",
    "note_chunk": "分段笔记",
    "note_writer": "笔记写作",
    "note_review": "笔记审阅",
    "board_recognition": "板书识别",
    "compaction": "上下文压缩",
}


def field(
    name: str,
    label: str,
    value: Any = "",
    *,
    type: FormInputType = FormInputType.STRING,
    input: FormInputType = FormInputType.TEXT,
    **options: Any,
) -> dict[str, Any]:
    condition = options.get("visible_when")
    if condition is not None:
        controller, allowed = condition
        options["visible_when"] = {
            "key": controller,
            "values": [str(item) for item in allowed],
        }
    return {
        "key": name,
        "name": name,
        "label": label,
        "type": type,
        "input": input,
        "value": value,
        **options,
    }


def number(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def size(value: Any) -> str:
    """Compact token counts: 1000000 -> 1M, 128000 -> 128K, 4096 -> 4.1K."""

    count = number(value)
    if count <= 0:
        return "—"
    for unit, scale in (("M", 1_000_000), ("K", 1_000)):
        if count >= scale:
            text = f"{count / scale:.1f}".rstrip("0").rstrip(".")
            return f"{text}{unit}"
    return str(count)


def role_titles(roles: list[str]) -> str:
    """Render role ids for messages: 默认（default） reads better than default."""

    return "、".join(
        f"{ROLE_TITLES[role]}（{role}）" if role in ROLE_TITLES else role
        for role in roles
    )


def selector_parts(selector: str) -> tuple[str, str]:
    """Split ``provider/model:level`` without mistaking the model for a level."""

    if "," in selector:
        return selector, ""
    target, separator, level = selector.rpartition(":")
    return (target, level) if separator and level in THINKING_LEVELS else (selector, "")


def providers(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list(catalog.get("providers", []))


def resolved(catalog: Mapping[str, Any], role: str) -> dict[str, Any]:
    return dict(catalog.get("roles", {}).get(role, {}))


def roles_using(catalog: Mapping[str, Any], provider: str, model: str) -> list[str]:
    prefix = f"{provider}/{model}"
    roles = [
        role
        for role, selector in catalog.get("selectors", {}).items()
        if selector == prefix or selector.startswith(f"{prefix}:")
    ]
    # ``default`` governs the roles that inherit from it, so lead with it.
    return sorted(roles, key=lambda role: (role != "default", role))


def image_ok(catalog: Mapping[str, Any], role: str) -> bool:
    info = resolved(catalog, role)
    for provider in providers(catalog):
        if provider["id"] != info.get("provider"):
            continue
        for model in provider.get("models", []):
            if model["id"] == info.get("model"):
                return "image" in model.get("input", [])
    return False


def issues(catalog: Mapping[str, Any]) -> list[str]:
    """Everything that still prevents a run, in the order it should be fixed."""

    items = providers(catalog)
    if not items:
        return ["尚未配置提供方：在「提供方」页接入端点．"]
    found: list[str] = []
    for provider in items:
        if provider.get("auth") == "none" or provider.get("configured"):
            continue
        action = (
            "点击「关联套餐」完成浏览器授权"
            if provider.get("auth") == "oauth"
            else "填写 API Key"
        )
        found.append(f"提供方 `{provider['id']}` 未配置凭据：{action}．")
    if "board_recognition" in catalog.get("selectors", {}) and not image_ok(
        catalog, "board_recognition"
    ):
        found.append("板书识别角色使用的模型不支持 `image` 输入．")
    return found


class SettingsView:
    """Render LLM tasks and their provider-scoped content."""

    def __init__(
        self,
        catalog: Mapping[str, Any],
        checks: Mapping[str, Mapping[str, Any]] | None = None,
        login: Mapping[str, Any] | None = None,
        status: str = "",
    ) -> None:
        self.catalog = catalog
        self.checks = dict(checks or {})
        # An authorization in flight: {provider, uri, code, remaining}.
        self.login = dict(login) if login else None
        self.login_status = status

    # ---- pages

    def blocks(self) -> list[dict[str, Any]]:
        return [
            {"title": "概览", "blocks": self._overview()},
            {"title": "提供方", "blocks": self._provider_page()},
            {"title": "模型", "blocks": self._model_page()},
            {"title": "角色", "blocks": self._role_page()},
            {"title": "原始配置", "blocks": self._raw_page()},
        ]

    def _overview(self) -> list[dict[str, Any]]:
        items = providers(self.catalog)
        models = sum(len(item.get("models", [])) for item in items)
        missing = [
            item["id"]
            for item in items
            if item.get("auth") != "none" and not item.get("configured")
        ]
        blocks: list[dict[str, Any]] = [
            {"type": BlockType.MARKDOWN, "text": "### 配置摘要"},
            {
                "type": BlockType.KEYVALUE,
                "items": [
                    ["提供方", f"{len(items)} 个"],
                    ["模型", f"{models} 个"],
                    [
                        "凭据",
                        "全部已配置" if not missing else f"缺少：{'、'.join(missing)}",
                    ],
                ],
            },
        ]
        for problem in reversed(issues(self.catalog)):
            blocks.insert(
                0,
                {
                    "type": BlockType.HTML,
                    "html": f'<div class="alert theme-danger" role="alert">{escape(problem)}</div>',
                },
            )

        blocks.append({"type": BlockType.MARKDOWN, "text": "### 角色分配"})
        assignments = []
        for role in ROLE_TITLES:
            selector = self.catalog["selectors"][role]
            label = ROLE_TITLES.get(role, role)
            target = self._selector_text(role)
            value = target if selector == target else f"{selector} -> {target}"
            assignments.append([f"{label} ({role})", value])
        blocks.append({"type": BlockType.KEYVALUE, "items": assignments})
        return blocks

    def _selector_text(self, role: str) -> str:
        info = resolved(self.catalog, role)
        if not info:
            return "—"
        # Plain text: the key-value block does not render Markdown.
        text = f"{info.get('provider')}/{info.get('model')}"
        level = info.get("thinkingLevel")
        if level:
            text += f":{level}"
        return text

    def _provider_page(self) -> list[dict[str, Any]]:
        items = providers(self.catalog)
        blocks = self._login_panel()
        if items:
            blocks.append(
                {
                    "type": BlockType.TABLE,
                    "id": "llm:providers",
                    "editor": True,
                    "columns": [
                        {"key": "provider", "label": "提供方"},
                        {"key": "models", "label": "模型数"},
                        {"key": "auth", "label": "鉴权"},
                        {"key": "credentials", "label": "凭据"},
                    ],
                    "rows": [
                        {
                            "id": item["id"],
                            "provider": item["id"],
                            "models": len(item.get("models", [])),
                            "auth": AUTH_LABELS.get(item.get("auth"), item.get("auth", "")),
                            "credentials": (
                                "无需鉴权" if item.get("auth") == "none"
                                else "已配置" if item.get("configured") else "未配置"
                            ),
                            "details": [self._provider_form(item)],
                            "actions": self._provider_actions(item),
                        }
                        for item in items
                    ],
                    "row_actions": [
                        {
                            "label": "删除",
                            "icon": "trash",
                            "action": "llm.provider.remove",
                            "keys": ["provider"],
                            "class": "btn-text theme-danger btn-sm",
                        }
                    ],
                }
            )
        else:
            blocks.append(
                {"type": BlockType.MARKDOWN, "text": "尚无提供方．添加提供方后可配置连接与模型．"}
            )
        blocks.append(
            {
                "type": BlockType.DISCLOSURE,
                "id": "llm:add-provider",
                "title": "添加提供方",
                "open": not items,
                "blocks": [
                    {"type": BlockType.FORM, **self._add_form()},
                    self._new_provider_form(),
                    self._definition_form(
                        "llm:models-extensions", "配置扩展参数",
                        {"type": "object", "properties": {}},
                        {key: value for key, value in self.catalog["definitions"]["models"].items() if key != "providers"},
                        {"scope": "models_extensions"},
                    ),
                ],
            }
        )
        return blocks

    @staticmethod
    def _provider_tabs(
        key: str, label: str, tabs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": BlockType.TABS,
                "id": key,
                "label": label,
                "layout": "settings-pages",
                "hide_single": True,
                "tabs": tabs,
            }
        ]

    def _login_panel(self) -> list[dict[str, Any]]:
        """Keep the authorization URL and code available until login completes."""

        if self.login is None:
            if not self.login_status or not providers(self.catalog):
                return []
            return [{"type": BlockType.MARKDOWN, "text": self.login_status}]
        login = self.login
        pid = str(login.get("provider") or "")
        uri = str(login.get("uri") or "")
        code = str(login.get("code") or "")
        minutes = max(1, int(login.get("valid_for") or 0) // 60)
        lines = [
            "### 等待 OpenCode 授权",
            "",
            f"1. 在浏览器打开：[打开授权页面]({uri})（新标签页）",
            f"2. 确认页面显示的验证码：{code}",
            "",
            (
                f"链接与验证码 {minutes} 分钟内有效．授权完成后本页自动刷新．"
                "超时后可重新开始授权．"
            ),
            "",
            f"浏览器没有弹出时，复制这个地址：`{uri}`",
            "",
        ]
        return [
            {"type": BlockType.MARKDOWN, "text": "\n".join(lines)},
            {
                "type": BlockType.BUTTONS,
                "buttons": [
                    {
                        "label": "重新开始授权",
                        "action": "llm.opencode_login",
                        "value": {"provider": pid, "force": True},
                        "class": "btn-outline theme-secondary",
                    },
                    {
                        "label": "取消授权",
                        "action": "llm.opencode_cancel",
                        "value": {"provider": pid},
                        "class": "btn-outline theme-secondary",
                    },
                ],
            },
        ]

    def _provider_actions(
        self, provider: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        pid = provider["id"]
        actions: list[dict[str, Any]] = []
        if provider.get("auth") == "oauth" and not provider.get("configured") and self.login is None:
            actions.append(
                {
                    "label": "关联套餐",
                    "icon": "link",
                    "action": "llm.opencode_login",
                    "value": {"provider": pid},
                    "class": "btn-text theme-secondary btn-sm",
                }
            )
        if provider.get("auth") != "none" and provider.get("configured"):
            actions.append(
                {
                    "label": "删除凭据",
                    "icon": "key",
                    "action": "llm.logout",
                    "value": {"provider": pid},
                    "class": "btn-text theme-danger btn-sm",
                }
            )
        return actions

    def _provider_form(self, provider: Mapping[str, Any]) -> dict[str, Any]:
        pid = str(provider["id"])
        schema = self._provider_schema()
        schema["properties"]["models"]["x-hidden"] = True
        schema["properties"]["modelOverrides"]["x-hidden"] = True
        definition = self.catalog["definitions"]["models"]["providers"][pid]
        return self._definition_form(
            f"llm:provider:{pid}", f"连接与凭据 {pid}", schema, definition,
            {"scope": "provider", "provider": pid},
        )

    def _provider_schema(self) -> dict[str, Any]:
        schema = deepcopy(self.catalog["definition_schema"]["properties"]["providers"]["additionalProperties"])
        self._label_schema(schema)
        return schema

    @classmethod
    def _label_schema(cls, schema: dict[str, Any]) -> None:
        labels = {
            "baseUrl": "端点地址", "apiKey": "API Key", "api": "协议",
            "auth": "鉴权方式", "headers": "请求头", "compat": "协议兼容",
            "remoteCompaction": "服务端上下文压缩", "discovery": "端点模型发现",
            "models": "模型", "modelOverrides": "模型覆盖", "id": "模型 ID",
            "name": "显示名称", "kind": "模型用途", "reasoning": "支持思考",
            "thinking": "思考控制", "input": "输入类型", "contextWindow": "上下文窗口",
            "maxContextWindow": "最大上下文窗口", "maxTokens": "输出 token 上限",
            "cost": "模型价格", "promptCache": "提示词缓存", "tokenizer": "分词器",
            "supportsTools": "支持工具调用", "extraBody": "额外请求参数",
            "whenThinking": "思考模式兼容设置", "mode": "控制模式",
            "efforts": "支持的思考级别", "defaultLevel": "默认思考级别",
            "effortMap": "思考级别映射", "authHeader": "发送鉴权头",
            "requestMetadata": "请求元数据", "transport": "传输方式",
            "contextPromotionTarget": "扩展上下文目标", "compactionModel": "压缩模型",
        }
        for key, child in schema.get("properties", {}).items():
            child["title"] = labels.get(key, key)
            cls._label_schema(child)
        for key in ("items", "additionalProperties"):
            if isinstance(schema.get(key), dict):
                cls._label_schema(schema[key])

    @staticmethod
    def _definition_form(
        key: str, title: str, schema: dict[str, Any], value: Any, context: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "type": BlockType.SCHEMA_FORM, "id": key, "title": title,
            "schema": schema, "value": value, "context": context,
            "submit": {"action": "llm.definition.save", "label": "保存"},
        }

    def _new_provider_form(self) -> dict[str, Any]:
        schema = self._provider_schema()
        schema["properties"] = {
            "_provider_id": {"type": "string", "title": "提供方标识"},
            **schema["properties"],
        }
        schema["required"] = ["_provider_id", "models"]
        return self._definition_form(
            "llm:new-provider", "手动配置提供方", schema,
            {"_provider_id": "", "models": [{"id": ""}]},
            {"scope": "provider", "provider": ""},
        )

    @staticmethod
    def _add_form() -> dict[str, Any]:
        custom = ("preset", ("custom",))
        return {
            "description": "保存后自动获取端点模型．",
            "fields": [
                field(
                    "preset",
                    "预设",
                    PROVIDER_PRESETS[0][0],
                    type=FormInputType.SELECT,
                    input=FormInputType.SELECT,
                    options=[value for value, _label in PROVIDER_PRESETS],
                    option_items=[
                        {"value": value, "label": label}
                        for value, label in PROVIDER_PRESETS
                    ],
                    description="OpenCode Go 使用浏览器授权．",
                    section="提供方",
                ),
                field(
                    "provider",
                    "提供方标识",
                    "",
                    section="提供方",
                    placeholder="例如 lecture",
                    description="模型选择器的提供方前缀，创建后不可修改",
                ),
                field(
                    "base_url",
                    "端点地址",
                    "",
                    visible_when=custom,
                    placeholder="https://example.com/v1",
                    section="连接",
                ),
                field(
                    "api",
                    "协议",
                    API_OPTIONS[0],
                    type=FormInputType.SELECT,
                    input=FormInputType.SELECT,
                    options=list(API_OPTIONS),
                    visible_when=custom,
                    section="连接",
                ),
                field(
                    "auth",
                    "鉴权方式",
                    AUTH_OPTIONS[0],
                    type=FormInputType.SELECT,
                    input=FormInputType.SELECT,
                    options=list(AUTH_OPTIONS),
                    visible_when=custom,
                    section="凭据",
                ),
                field(
                    "key",
                    "API Key",
                    input=FormInputType.PASSWORD,
                    visible_when=custom,
                    description="可留空，之后在提供方编辑器中填写",
                    section="凭据",
                ),
            ],
            "submit": {"label": "保存提供方", "action": "llm.provider.save"},
        }

    def _model_page(self) -> list[dict[str, Any]]:
        items = providers(self.catalog)
        if not items:
            return [
                {
                    "type": BlockType.MARKDOWN,
                    "text": "尚无提供方．请先在「提供方」页接入端点．",
                }
            ]
        return self._provider_tabs(
            "llm:models",
            "模型提供方",
            [
                {
                    "id": item["id"],
                    "title": item["id"],
                    "blocks": self._provider_models(item),
                }
                for item in items
            ],
        )

    def _provider_models(self, provider: Mapping[str, Any]) -> list[dict[str, Any]]:
        pid = provider["id"]
        blocks: list[dict[str, Any]] = []
        blocks.extend(self._model_table(provider))
        model_schema = self._provider_schema()["properties"]["models"]["items"]
        blocks.append({
            "type": BlockType.DISCLOSURE, "id": f"llm:add-model:{pid}",
            "title": "添加模型",
            "blocks": [self._definition_form(
                f"llm:new-model:{pid}", "手动配置模型", model_schema, {"id": ""},
                {"scope": "model", "provider": pid, "index": -1},
            )],
        })
        overrides = self.catalog["definitions"]["models"]["providers"][pid].get("modelOverrides", {})
        override_schema = self._provider_schema()["properties"]["modelOverrides"]["additionalProperties"]
        blocks.append({
            "type": BlockType.DISCLOSURE, "id": f"llm:overrides:{pid}",
            "title": "模型覆盖",
            "blocks": [self._definition_form(
                f"llm:override:{pid}", "覆盖模型设置",
                {"type": "object", "properties": {}, "additionalProperties": override_schema},
                overrides, {"scope": "overrides", "provider": pid},
            )],
        })
        blocks.extend(
            [
                {"type": BlockType.MARKDOWN, "text": "### 发现端点模型"},
                {
                    "type": BlockType.BUTTONS,
                    "buttons": [
                        {
                            "label": "检查端点模型",
                            "action": "llm.check",
                            "value": {"provider": pid},
                            "class": "btn-outline theme-secondary",
                        }
                    ],
                },
                *self._discovery(provider),
            ]
        )
        return blocks

    def _discovery(self, provider: Mapping[str, Any]) -> list[dict[str, Any]]:
        pid = str(provider["id"])
        check = self.checks.get(pid)
        if check is None:
            return [
                {
                    "type": BlockType.MARKDOWN,
                    "text": "尚未检查端点模型．检查结果只列出本地未添加的模型．",
                }
            ]
        if check.get("error"):
            return [
                {
                    "type": BlockType.MARKDOWN,
                    "text": f"检查失败：{check['error']}．检查端点地址与凭据后重试．",
                }
            ]
        known = {model["id"] for model in provider.get("models", [])}
        missing = [item for item in check.get("models", []) if item["id"] not in known]
        if not missing:
            return [
                {
                    "type": BlockType.MARKDOWN,
                    "text": (
                        f"端点返回 {len(check.get('models', []))} 个模型，"
                        "本地已全部添加．"
                    ),
                }
            ]
        return [
            {
                "type": BlockType.MARKDOWN,
                "text": (
                    f"端点返回 {len(check.get('models', []))} 个模型，"
                    f"其中 {len(missing)} 个尚未添加："
                ),
            },
            {
                "type": BlockType.TABLE,
                "columns": [
                    {"key": "model", "label": "模型"},
                    {"key": "name", "label": "名称"},
                    {"key": "context", "label": "上下文"},
                    {"key": "input", "label": "输入"},
                ],
                "rows": [
                    {
                        "model": item["id"],
                        "name": item.get("name") or "—",
                        "context": size(item.get("contextWindow")),
                        "input": "/".join(item.get("input", [])),
                    }
                    for item in missing
                ],
                "row_actions": [
                    {
                        "label": "添加",
                        "action": "llm.model.add",
                        "keys": ["model"],
                        "value": {"provider": pid},
                        "class": "btn-outline theme-secondary btn-sm",
                    }
                ],
            },
            {
                "type": BlockType.BUTTONS,
                "buttons": [
                    {
                        "label": f"添加全部（{len(missing)}）",
                        "action": "llm.model.add_all",
                        "value": {"provider": pid},
                        "class": "btn-outline theme-secondary",
                    }
                ],
            },
        ]

    def _model_table(self, provider: Mapping[str, Any]) -> list[dict[str, Any]]:
        pid = str(provider["id"])
        models = list(provider.get("models", []))
        blocks: list[dict[str, Any]] = [
            {"type": BlockType.MARKDOWN, "text": f"### {pid} 的已配置模型"}
        ]
        if not models:
            blocks.append(
                {
                    "type": BlockType.MARKDOWN,
                    "text": "尚无模型．检查端点模型后可添加模型．",
                }
            )
            return blocks
        rows = []
        for index, model in enumerate(models):
            roles = roles_using(self.catalog, pid, model["id"])
            rows.append(
                {
                    "id": model["id"],
                    "model": model["id"],
                    "context": size(model.get("contextWindow")),
                    "output": size(model.get("maxTokens")),
                    "input": "/".join(model.get("input", [])) or "—",
                    "reasoning": "是" if model.get("reasoning") else "否",
                    "roles": "、".join(roles) if roles else "—",
                    "details": self._model_editor(provider, model, index),
                }
            )
        blocks.append(
            {
                "type": BlockType.TABLE,
                "id": f"llm:models:{pid}",
                "editor": True,
                "columns": [
                    {"key": "model", "label": "模型"},
                    {"key": "context", "label": "上下文"},
                    {"key": "output", "label": "输出"},
                    {"key": "input", "label": "输入"},
                    {"key": "reasoning", "label": "推理"},
                    {"key": "roles", "label": "使用角色"},
                ],
                "rows": rows,
                "row_actions": [
                    {
                        "label": "删除",
                        "icon": "trash",
                        "action": "llm.model.remove",
                        "keys": ["model"],
                        "value": {"provider": pid},
                        "class": "btn-text theme-danger btn-sm",
                    },
                ],
            }
        )
        return blocks

    def _model_editor(
        self, provider: Mapping[str, Any], model: Mapping[str, Any], index: int
    ) -> list[dict[str, Any]]:
        pid = provider["id"]
        schema = self._provider_schema()["properties"]["models"]["items"]
        return [self._definition_form(
            f"llm:model:{pid}:{model['id']}", f"模型 {model['id']}", schema, dict(model),
            {"scope": "model", "provider": pid, "index": index},
        )]

    def _role_page(self) -> list[dict[str, Any]]:
        selectors = dict(self.catalog.get("selectors", {}))
        if not selectors:
            return [{"type": BlockType.MARKDOWN, "text": "尚未配置角色．"}]
        blocks: list[dict[str, Any]] = []
        rows = []
        for name in ROLE_TITLES:
            status = ""
            if name == "board_recognition":
                status = (
                    "图像可用" if image_ok(self.catalog, name) else "缺少 image 输入"
                )
            rows.append(
                {
                    "id": name,
                    "role": name,
                    "title": ROLE_TITLES.get(name, name),
                    "selector": selectors[name],
                    "active": self._selector_text(name),
                    "status": status,
                    "details": self._role_editor(name),
                }
            )
        blocks.append(
            {
                "type": BlockType.TABLE,
                "id": "llm:roles",
                "editor": True,
                "columns": [
                    {"key": "title", "label": "角色"},
                    {"key": "role", "label": "标识"},
                    {"key": "selector", "label": "设置"},
                    {"key": "active", "label": "生效模型"},
                    {"key": "status", "label": "状态"},
                ],
                "rows": rows,
            }
        )
        return blocks

    def _role_editor(self, role: str) -> list[dict[str, Any]]:
        selectors = dict(self.catalog.get("selectors", {}))
        selector = selectors[role]
        target, level = selector_parts(selector)
        note = "保存后对新会话生效．"
        if role == "board_recognition" and not image_ok(self.catalog, role):
            note += "板书识别需要支持 image 输入的模型．"
        return [
            {
                "type": BlockType.FORM,
                "id": f"llm:role:{role}",
                "title": f"编辑角色 {ROLE_TITLES.get(role, role)}",
                "description": note,
                "fields": [
                    field("role", "角色", role, readonly=True, hidden=True),
                    field(
                        "model",
                        "模型或角色引用",
                        target,
                        section="模型分配",
                        description="模型选择器或 @角色引用，支持逗号分隔的候选序列．",
                    ),
                    field(
                        "level",
                        "思考级别",
                        level,
                        type=FormInputType.SELECT,
                        input=FormInputType.SELECT,
                        options=["", *THINKING_LEVELS],
                        option_items=[
                            {"value": value, "label": value or "-"}
                            for value in ("", *THINKING_LEVELS)
                        ],
                        section="模型分配",
                        description="- 表示不指定思考级别；off 表示关闭思考．",
                    ),
                ],
                "submit": {"label": "保存角色", "action": "llm.role.save"},
            }
        ]

    def _raw_page(self) -> list[dict[str, Any]]:
        return [
            {
                "type": BlockType.FORM,
                "description": "保存会整体替换模型与角色配置．通过校验后才会写入．",
                "fields": [
                    field(
                        "models_json",
                        "模型与提供方 JSON",
                        json.dumps(self.catalog["definitions"]["models"], ensure_ascii=False, indent=2),
                        type=FormInputType.TEXT,
                        input=FormInputType.TEXTAREA,
                        rows=18,
                    ),
                    field(
                        "roles_json",
                        "角色 JSON",
                        json.dumps(self.catalog["definitions"]["roles"], ensure_ascii=False, indent=2),
                        type=FormInputType.TEXT,
                        input=FormInputType.TEXTAREA,
                        rows=12,
                    ),
                ],
                "submit": {"label": "保存配置", "action": "llm.configure"},
            },
        ]


__all__ = [
    "API_OPTIONS",
    "AUTH_OPTIONS",
    "INPUT_FORMATS",
    "PAGE_ID",
    "PRESETS",
    "PROVIDER_PRESETS",
    "ROLE_TITLES",
    "THINKING_LEVELS",
    "SettingsView",
    "field",
    "image_ok",
    "issues",
    "number",
    "providers",
    "resolved",
    "role_titles",
    "roles_using",
    "selector_parts",
    "size",
]
