"""Render LLM collections and selected editors from the runtime catalog."""

from __future__ import annotations

from html import escape
from typing import Any, Mapping

from core.ui import BlockType, FormInputType

PAGE_ID = "llm:configuration"

# Endpoint presets: protocol, auth mode and URL are decided here so the UI never
# asks the user to remember spellings.
PROVIDER_PRESETS: tuple[tuple[str, str], ...] = (
    ("custom", "OpenAI 兼容端点"),
    ("opencode-go", "OpenCode Go"),
    ("deepseek", "DeepSeek"),
)
PRESETS: dict[str, dict[str, str]] = {
    "opencode-go": {
        "base_url": "https://opencode.ai/inference/go/openai/v1",
        "api": "openai-completions",
        "auth": "oauth",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "api": "openai-completions",
        "auth": "apiKey",
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
    "max",
)
ROLE_TITLES: dict[str, str] = {
    "default": "默认",
    "ask_instant": "快速问答",
    "ask_research": "研究问答",
    "note_chunk": "笔记分段",
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

    target, _, level = selector.partition(":")
    return target, level


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
    """Render the LLM section for one catalog and one selection."""

    def __init__(
        self,
        catalog: Mapping[str, Any],
        selection: Mapping[str, str],
        checks: Mapping[str, Mapping[str, Any]] | None = None,
        login: Mapping[str, Any] | None = None,
        status: str = "",
    ) -> None:
        self.catalog = catalog
        self.selection = dict(selection)
        self.checks = dict(checks or {})
        # An authorization in flight: {provider, uri, code, remaining}.
        self.login = dict(login) if login else None
        self.login_status = status

    # ---- what is being edited

    def provider(self) -> dict[str, Any] | None:
        items = providers(self.catalog)
        for item in items:
            if item["id"] == self.selection.get("provider"):
                return item
        return items[0] if items else None

    def model(self, provider: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if provider is None:
            return None
        models = list(provider.get("models", []))
        for model in models:
            if model["id"] == self.selection.get("model"):
                return model
        return models[0] if models else None

    def role(self) -> str:
        selectors = self.catalog.get("selectors", {})
        if self.selection.get("role") in selectors:
            return self.selection["role"]
        if "default" in selectors:
            return "default"
        return next(iter(selectors), "")

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
        for role, selector in self.catalog.get("selectors", {}).items():
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
        existing: list[dict[str, Any]] = []
        if items:
            existing.append(
                {
                    "type": BlockType.TABLE,
                    "columns": [
                        {"key": "provider", "label": "提供方"},
                        {"key": "endpoint", "label": "端点地址"},
                        {"key": "auth", "label": "鉴权方式"},
                        {"key": "status", "label": "凭据状态"},
                        {"key": "models", "label": "模型数"},
                    ],
                    "rows": [
                        {
                            "provider": item["id"],
                            "endpoint": item.get("baseUrl") or "未填写",
                            "auth": AUTH_LABELS.get(
                                str(item.get("auth")), str(item.get("auth"))
                            ),
                            "status": (
                                "无需凭据"
                                if item.get("auth") == "none"
                                else "已配置"
                                if item.get("configured")
                                else "未配置"
                            ),
                            "models": len(item.get("models", [])),
                        }
                        for item in items
                    ],
                    "row_actions": [
                        {
                            "label": "编辑",
                            "action": "llm.select.provider",
                            "keys": ["provider"],
                            "class": "btn-outline theme-secondary btn-sm",
                        }
                    ],
                }
            )
            provider = self.provider()
            assert provider is not None
            existing.append({"type": BlockType.FORM, **self._provider_form(provider)})
            existing.extend(self._provider_actions(provider, len(items) > 1))
        else:
            existing.append(
                {
                    "type": BlockType.MARKDOWN,
                    "text": "尚无提供方．在「接入提供方」中添加端点．",
                }
            )
        blocks.append(
            {
                "type": BlockType.TABS,
                "id": "llm:providers",
                "tabs": [
                    {"title": "已配置提供方", "blocks": existing},
                    {
                        "title": "接入提供方",
                        "blocks": [{"type": BlockType.FORM, **self._add_form()}],
                    },
                ],
            }
        )
        return blocks

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

    @staticmethod
    def _provider_actions(
        provider: Mapping[str, Any], removable: bool
    ) -> list[dict[str, Any]]:
        pid = provider["id"]
        actions: list[dict[str, Any]] = []
        if provider.get("auth") == "oauth":
            actions.append(
                {
                    "label": "关联套餐",
                    "action": "llm.opencode_login",
                    "value": {"provider": pid},
                    "class": "btn-outline theme-secondary",
                }
            )
        if provider.get("configured"):
            actions.append(
                {
                    "label": "删除凭据",
                    "action": "llm.logout",
                    "value": {"provider": pid},
                    "class": "btn-outline theme-danger",
                }
            )
        if removable:
            actions.append(
                {
                    "label": "删除提供方",
                    "action": "llm.provider.remove",
                    "value": {"provider": pid},
                    "class": "btn-outline theme-danger",
                }
            )
        if not actions:
            return []
        return [
            {"type": BlockType.MARKDOWN, "text": "#### 授权与删除"},
            {"type": BlockType.BUTTONS, "buttons": actions},
        ]

    @staticmethod
    def _provider_form(provider: Mapping[str, Any]) -> dict[str, Any]:
        pid = str(provider["id"])
        return {
            "title": f"编辑提供方 {pid}",
            "description": "提供方标识创建后不可修改．",
            "fields": [
                field("provider", "提供方", pid, readonly=True, hidden=True),
                field(
                    "base_url", "端点地址", provider.get("baseUrl", ""), section="连接"
                ),
                field(
                    "api",
                    "协议",
                    provider.get("api", API_OPTIONS[0]),
                    type=FormInputType.SELECT,
                    input=FormInputType.SELECT,
                    options=list(API_OPTIONS),
                    section="连接",
                ),
                field(
                    "auth",
                    "鉴权方式",
                    provider.get("auth", AUTH_OPTIONS[0]),
                    type=FormInputType.SELECT,
                    input=FormInputType.SELECT,
                    options=list(AUTH_OPTIONS),
                    section="凭据",
                ),
                field(
                    "key",
                    "API Key",
                    input=FormInputType.PASSWORD,
                    visible_when=("auth", ("apiKey",)),
                    section="凭据",
                    description="留空保留已保存的凭据",
                ),
            ],
            "submit": {"label": "保存提供方", "action": "llm.provider.save"},
        }

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
                    visible_when=("preset", ("deepseek", "custom")),
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
        provider = self.provider()
        assert provider is not None
        pid = provider["id"]
        blocks: list[dict[str, Any]] = [
            {
                "type": BlockType.FORM,
                "fields": [
                    field(
                        "provider",
                        "提供方",
                        pid,
                        type=FormInputType.SELECT,
                        input=FormInputType.SELECT,
                        options=[item["id"] for item in items],
                    )
                ],
                "submit": {
                    "label": "查看模型",
                    "action": "llm.select.provider",
                    "class": "btn-outline theme-secondary",
                },
            }
        ]
        blocks.extend(self._model_table(provider))
        blocks.extend(self._model_editor(provider))
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
        for model in models:
            roles = roles_using(self.catalog, pid, model["id"])
            rows.append(
                {
                    "model": model["id"],
                    "context": size(model.get("contextWindow")),
                    "output": size(model.get("maxTokens")),
                    "input": "/".join(model.get("input", [])) or "—",
                    "reasoning": "是" if model.get("reasoning") else "否",
                    "roles": "、".join(roles) if roles else "—",
                }
            )
        blocks.append(
            {
                "type": BlockType.TABLE,
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
                        "label": "编辑",
                        "action": "llm.select.model",
                        "keys": ["model"],
                        "value": {"provider": pid},
                        "class": "btn-outline theme-secondary btn-sm",
                    },
                    {
                        "label": "删除",
                        "action": "llm.model.remove",
                        "keys": ["model"],
                        "value": {"provider": pid},
                        "class": "btn-outline theme-danger btn-sm",
                    },
                ],
            }
        )
        return blocks

    def _model_editor(self, provider: Mapping[str, Any]) -> list[dict[str, Any]]:
        model = self.model(provider)
        if model is None:
            return []
        pid, mid = provider["id"], model["id"]
        formats = set(model.get("input", []))
        roles = roles_using(self.catalog, pid, mid)
        hint = f"使用角色：{role_titles(roles)}" if roles else ""
        return [
            {
                "type": BlockType.FORM,
                "title": f"编辑模型 {pid}/{mid}",
                "description": hint,
                "fields": [
                    field("provider", "提供方", pid, readonly=True, hidden=True),
                    field("model", "模型", mid, readonly=True, hidden=True),
                    field(
                        "context_window",
                        "上下文窗口",
                        number(model.get("contextWindow")),
                        type=FormInputType.NUMBER,
                        input=FormInputType.NUMBER,
                        section="Token 限制",
                        description="请求上下文的 token 上限．留空沿用端点或默认值．",
                    ),
                    field(
                        "max_tokens",
                        "输出上限",
                        number(model.get("maxTokens")),
                        type=FormInputType.NUMBER,
                        input=FormInputType.NUMBER,
                        section="Token 限制",
                        description="单次回复的 token 上限",
                    ),
                    field(
                        "input_text",
                        "输入文本",
                        "text" in formats,
                        type=FormInputType.CHECKBOX,
                        input=FormInputType.CHECKBOX,
                        section="模型能力",
                    ),
                    field(
                        "input_image",
                        "输入图像",
                        "image" in formats,
                        type=FormInputType.CHECKBOX,
                        input=FormInputType.CHECKBOX,
                        section="模型能力",
                        description="板书识别要求所选模型勾选此项",
                    ),
                    field(
                        "reasoning",
                        "推理模型",
                        bool(model.get("reasoning")),
                        type=FormInputType.CHECKBOX,
                        input=FormInputType.CHECKBOX,
                        section="模型能力",
                        description="支持思考级别的模型可在角色中设置 :high 等后缀",
                    ),
                ],
                "submit": {"label": "保存模型", "action": "llm.model.save"},
            },
        ]

    def _role_page(self) -> list[dict[str, Any]]:
        selectors = dict(self.catalog.get("selectors", {}))
        if not selectors:
            return [{"type": BlockType.MARKDOWN, "text": "尚未配置角色．"}]
        options: list[str] = ["@default"]
        for provider in providers(self.catalog):
            for model in provider.get("models", []):
                options.append(f"{provider['id']}/{model['id']}")
        role = self.role()
        blocks: list[dict[str, Any]] = []
        rows = []
        for name in selectors:
            status = ""
            if name == "board_recognition":
                status = (
                    "图像可用" if image_ok(self.catalog, name) else "缺少 image 输入"
                )
            rows.append(
                {
                    "role": name,
                    "title": ROLE_TITLES.get(name, name),
                    "selector": selectors.get(name, "@default"),
                    "active": self._selector_text(name),
                    "status": status,
                }
            )
        blocks.append(
            {
                "type": BlockType.TABLE,
                "columns": [
                    {"key": "title", "label": "角色"},
                    {"key": "role", "label": "标识"},
                    {"key": "selector", "label": "设置"},
                    {"key": "active", "label": "生效模型"},
                    {"key": "status", "label": "状态"},
                ],
                "rows": rows,
                "row_actions": [
                    {
                        "label": "编辑",
                        "action": "llm.select.role",
                        "keys": ["role"],
                        "class": "btn-outline theme-secondary btn-sm",
                    }
                ],
            }
        )
        if not role:
            return blocks
        selector = selectors.get(role, "@default")
        target, level = selector_parts(selector)
        if target.startswith("@"):
            target = "@default"
            level = level or "off"
        if target not in options:
            target = options[0]
        note = "保存后对新会话生效．"
        if role == "board_recognition" and not image_ok(self.catalog, role):
            note += "板书识别需要支持 image 输入的模型．"
        blocks.append(
            {
                "type": BlockType.FORM,
                "title": f"编辑角色 {ROLE_TITLES.get(role, role)}",
                "description": note,
                "fields": [
                    field("role", "角色", role, readonly=True, hidden=True),
                    field(
                        "model",
                        "模型",
                        target,
                        type=FormInputType.SELECT,
                        input=FormInputType.SELECT,
                        options=options,
                        section="模型分配",
                        description="@default 继承默认角色的模型",
                    ),
                    field(
                        "level",
                        "思考级别",
                        level or "off",
                        type=FormInputType.SELECT,
                        input=FormInputType.SELECT,
                        options=list(THINKING_LEVELS),
                        section="模型分配",
                        description="模型必须支持所选思考级别",
                    ),
                ],
                "submit": {"label": "保存角色", "action": "llm.role.save"},
            }
        )
        return blocks

    def _raw_page(self) -> list[dict[str, Any]]:
        return [
            {
                "type": BlockType.FORM,
                "description": "保存会整体替换 models.yml 和 roles.yml．配置通过校验后才会写入．",
                "fields": [
                    field(
                        "models_yaml",
                        "models.yml",
                        self.catalog.get("models_yaml", ""),
                        type=FormInputType.TEXT,
                        input=FormInputType.TEXTAREA,
                        rows=18,
                        section="模型与提供方",
                    ),
                    field(
                        "roles_yaml",
                        "roles.yml",
                        self.catalog.get("roles_yaml", ""),
                        type=FormInputType.TEXT,
                        input=FormInputType.TEXTAREA,
                        rows=12,
                        section="角色分配",
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
