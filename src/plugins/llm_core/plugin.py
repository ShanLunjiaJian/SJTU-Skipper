from __future__ import annotations

import logging
import threading
import time
import webbrowser
from enum import StrEnum
from typing import Any, Mapping

from agent_runtime.client import RuntimeClient
from core.agent_types import AgentRole, IpcMethod
from core.plugin import Plugin, PluginContext
from core.ui import BlockType, FormInputType
from integrations.opencode_go import (
    OpenCodeGoError,
    poll_device_token,
    start_device_flow,
)

logger = logging.getLogger("lecture_agent.plugin.llm")

_ACTIONS = (
    "llm.configure",
    "llm.credentials",
    "llm.logout",
    "llm.opencode_login",
)


class ProviderAuth(StrEnum):
    NONE = "none"
    API_KEY = "apiKey"
    OAUTH = "oauth"


class CredentialType(StrEnum):
    API_KEY = "api_key"
    OAUTH = "oauth"


class ProviderSource(StrEnum):
    CONFIG = "config"
    PROJECT = "project"


def _values(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = payload.get("values")
    return nested if isinstance(nested, Mapping) else payload


def _field(
    name: str,
    label: str,
    value: Any = "",
    *,
    type: FormInputType = FormInputType.STRING,
    input: FormInputType = FormInputType.TEXT,
    **options: Any,
) -> dict[str, Any]:
    return {
        "key": name,
        "name": name,
        "label": label,
        "type": type,
        "input": input,
        "value": value,
        **options,
    }


class LLMPlugin(Plugin):
    id = "llm"
    requires = ("settings",)
    provides = ("llm",)

    def __init__(self) -> None:
        self._settings: Any = None
        self._runtime: RuntimeClient | None = None
        self._ctx: PluginContext | None = None
        self._login_lock = threading.RLock()
        self._login_thread: threading.Thread | None = None
        self._login_cancel = threading.Event()
        self._login_provider = ""
        self._login_status = ""

    def setup(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._settings = ctx.require("settings")
        runtime = RuntimeClient(ctx.data_dir / "llm_runtime")
        self._runtime = runtime
        ctx.register_service("llm", runtime)
        handlers = (
            self._handle_configure,
            self._handle_credentials,
            self._handle_logout,
            self._handle_opencode_login,
        )
        for action, handler in zip(_ACTIONS, handlers):
            ctx.ui.register_action(action, handler, plugin_id=self.id)
        self._settings.register_section("llm", "LLM", self._render_settings)

    def teardown(self) -> None:
        self._login_cancel.set()
        if self._settings is not None:
            self._settings.unregister_section("llm")
        if self._ctx is not None:
            for action in _ACTIONS:
                self._ctx.ui.unregister_action(action)
        if self._runtime is not None:
            self._runtime.close()
        self._runtime = None

    def _refresh(self) -> None:
        if self._ctx is not None:
            self._ctx.ui.refresh("llm:configuration")

    def _call(self, method: IpcMethod, **params: Any) -> dict[str, Any]:
        if self._runtime is None:
            raise RuntimeError("The LLM runtime is unavailable")
        result = self._runtime.call(method, **params)
        if method == IpcMethod.INFO:
            result["roles"] = {
                AgentRole(role): resolved
                for role, resolved in result.get("roles", {}).items()
            }
            for provider in result.get("providers", []):
                provider["auth"] = ProviderAuth(provider["auth"])
                if provider.get("source"):
                    provider["source"] = ProviderSource(provider["source"])
        return result

    def _render_settings(self) -> list[dict[str, Any]]:
        info = self._call(IpcMethod.INFO)
        blocks: list[dict[str, Any]] = [
            {
                "type": BlockType.FORM,
                "fields": [
                    _field(
                        "models_yaml",
                        "models.yml",
                        info["models_yaml"],
                        type=FormInputType.TEXT,
                        input=FormInputType.TEXTAREA,
                    ),
                    _field(
                        "roles_yaml",
                        "roles.yml",
                        info["roles_yaml"],
                        type=FormInputType.TEXT,
                        input=FormInputType.TEXTAREA,
                    ),
                ],
                "submit": {"label": "保存配置", "action": "llm.configure"},
            }
        ]
        roles = info.get("roles", {})
        lines = ["### 角色模型"]
        for role, resolved in roles.items():
            selector = f"{resolved['provider']}/{resolved['model']}"
            level = resolved.get("thinkingLevel")
            if level:
                selector += f":{level}"
            lines.append(f"- `{role}`: `{selector}`")
        blocks.append({"type": BlockType.MARKDOWN, "text": "\n".join(lines)})
        providers = info.get("providers", [])
        lines = ["### 提供方凭据"]
        for provider in providers:
            status = "已配置" if provider.get("configured") else "未配置"
            if provider.get("auth") == ProviderAuth.NONE:
                status = "无需鉴权"
            source = provider.get("source") or "none"
            lines.append(f"- `{provider['id']}`: {status}（{source}）")
        if self._login_status:
            lines.append(self._login_status)
        blocks.append({"type": BlockType.MARKDOWN, "text": "\n".join(lines)})

        def provider_field(items: list[dict[str, Any]]) -> dict[str, Any]:
            ids = [item["id"] for item in items]
            return _field(
                "provider",
                "提供方",
                ids[0] if ids else "",
                type=FormInputType.SELECT,
                input=FormInputType.SELECT,
                options=ids,
            )

        api_providers = [
            item for item in providers if item.get("auth") == ProviderAuth.API_KEY
        ]
        if api_providers:
            blocks.append(
                {
                    "type": BlockType.FORM,
                    "fields": [
                        provider_field(api_providers),
                        _field("key", "API Key", input=FormInputType.PASSWORD),
                    ],
                    "submit": {"label": "保存密钥", "action": "llm.credentials"},
                }
            )
        oauth = [item for item in providers if item.get("auth") == ProviderAuth.OAUTH]
        if oauth:
            blocks.append(
                {
                    "type": BlockType.FORM,
                    "fields": [provider_field(oauth)],
                    "submit": {"label": "关联套餐", "action": "llm.opencode_login"},
                }
            )
        configured = [
            item for item in providers if item.get("auth") != ProviderAuth.NONE
        ]
        if configured:
            blocks.append(
                {
                    "type": BlockType.FORM,
                    "fields": [provider_field(configured)],
                    "submit": {"label": "删除凭据", "action": "llm.logout"},
                }
            )
        return blocks

    def _handle_configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        values = _values(payload)
        models = values.get("models_yaml")
        roles = values.get("roles_yaml")
        if not isinstance(models, str) or not isinstance(roles, str):
            return {
                "ok": False,
                "message": "请填写两份 YAML 配置后保存．",
                "refresh": False,
            }
        try:
            self._call(IpcMethod.CONFIGURE, models=models, roles=roles)
        except Exception as exc:
            return {
                "ok": False,
                "message": f"保存失败：{exc}．修改配置后重试．",
                "refresh": False,
            }
        self._refresh()
        return {
            "ok": True,
            "message": "已保存配置，新会话使用新配置．",
            "refresh": True,
        }

    def _handle_credentials(self, payload: dict[str, Any]) -> dict[str, Any]:
        values = _values(payload)
        provider = str(values.get("provider") or "")
        key = values.get("key")
        if not provider or not isinstance(key, str) or not key.strip():
            return {
                "ok": False,
                "message": "请选择提供方并填写密钥．",
                "refresh": False,
            }
        try:
            self._call(IpcMethod.CREDENTIALS, provider=provider, key=key)
        except Exception:
            # Provider errors can contain request data. Never echo credential values.
            logger.error("failed to save provider credentials")
            return {
                "ok": False,
                "message": "密钥保存失败．检查提供方配置后重试．",
                "refresh": False,
            }
        self._refresh()
        return {"ok": True, "message": "已保存密钥．", "refresh": True}

    def _handle_logout(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = str(_values(payload).get("provider") or "")
        if not provider:
            return {"ok": False, "message": "请选择提供方．", "refresh": False}
        try:
            with self._login_lock:
                if self._login_provider == provider:
                    self._login_cancel.set()
                    self._login_status = ""
                self._call(IpcMethod.LOGOUT, provider=provider)
        except Exception:
            return {"ok": False, "message": "凭据删除失败．请重试．", "refresh": False}
        self._refresh()
        return {"ok": True, "message": "已删除凭据．", "refresh": True}

    def _handle_opencode_login(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = str(_values(payload).get("provider") or "")
        try:
            allowed = {
                item["id"]
                for item in self._call(IpcMethod.INFO).get("providers", [])
                if item.get("auth") == ProviderAuth.OAUTH
            }
            if provider not in allowed:
                return {
                    "ok": False,
                    "message": "请选择 OpenCode 提供方．",
                    "refresh": False,
                }
            with self._login_lock:
                if self._login_thread is not None and self._login_thread.is_alive():
                    return {
                        "ok": True,
                        "message": "授权正在进行，请完成浏览器中的授权．",
                        "refresh": True,
                    }
                flow = start_device_flow()
                self._login_cancel = threading.Event()
                self._login_provider = provider
                self._login_status = "等待 OpenCode 授权．"
                self._login_thread = threading.Thread(
                    target=self._poll_login,
                    args=(provider, flow, self._login_cancel),
                    name="opencode-device-login",
                    daemon=True,
                )
                self._login_thread.start()
        except Exception:
            return {
                "ok": False,
                "message": "无法启动 OpenCode 授权．检查网络后重试．",
                "refresh": False,
            }
        try:
            webbrowser.open(flow.verification_uri_complete)
        except Exception:
            pass
        return {
            "ok": True,
            "message": f"请打开 {flow.verification_uri_complete} 完成授权．验证码：{flow.user_code}",
            "refresh": True,
        }

    def _poll_login(self, provider: str, flow: Any, cancel: threading.Event) -> None:
        deadline = time.monotonic() + flow.expires_in
        try:
            while time.monotonic() < deadline and not cancel.is_set():
                tokens = poll_device_token(flow)
                if tokens is not None:
                    with self._login_lock:
                        if cancel.is_set():
                            return
                        self._call(
                            IpcMethod.IMPORT_CREDENTIALS,
                            provider=provider,
                            credential={
                                "type": CredentialType.OAUTH,
                                "access": tokens.access_token,
                                "refresh": tokens.refresh_token,
                                "expires": tokens.expires_at_ms,
                                "org_id": tokens.org_id,
                            },
                        )
                        self._login_status = "已关联 OpenCode 套餐．"
                    return
                cancel.wait(max(1, int(flow.interval)))
            if not cancel.is_set():
                self._login_status = "OpenCode 授权超时．请重新关联．"
        except (OpenCodeGoError, RuntimeError):
            self._login_status = "OpenCode 授权失败．请重新关联．"
        except Exception:
            self._login_status = "OpenCode 凭据保存失败．请重新关联．"
        finally:
            self._refresh()
