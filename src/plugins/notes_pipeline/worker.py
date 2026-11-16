"""Run one note agent in a fresh interpreter and return its conversation state."""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from llm.openai_compat import OpenAICompatibleProvider
from llm.service import LLMService
from llm.types import ProviderConfig
from plugins.agent.plugin import AgentService
from plugins.agent.session import AgentSession
from plugins.agent.workspace import FileWorkspace


SESSION_FIELDS = tuple(
    item.name
    for item in fields(AgentSession)
    if item.name not in ("workspace", "tools", "lock")
)


def workspace_state(workspace: FileWorkspace) -> dict[str, Any]:
    return {
        "files": {name: str(path) for name, path in workspace.to_dict().items()},
        "hidden": list(workspace._hidden),
        "readonly": list(workspace._readonly),
        "cache_dir": str(workspace._cache_dir) if workspace._cache_dir else None,
    }


def session_state(session: AgentSession) -> dict[str, Any]:
    return {
        "session": {name: getattr(session, name) for name in SESSION_FIELDS},
        "workspace": workspace_state(session.workspace),
        "tools": list(session.tools),
    }


def restore_workspace(state: dict[str, Any]) -> FileWorkspace:
    workspace = FileWorkspace(
        state["files"], cache_dir=state["cache_dir"], readonly=state["readonly"]
    )
    for name in state["hidden"]:
        workspace.register(name, workspace.path(name), hidden=True)
    return workspace


def run(payload: dict[str, Any], send) -> None:
    config = ProviderConfig(**payload["provider"])
    if config.provider not in ("", "openai", "openai-compatible"):
        raise ValueError(f"Unsupported note worker provider: {config.provider}")

    def emit(name, **data):
        send({"type": "event", "name": name, "data": data})

    llm = LLMService(
        config_provider=lambda _: config,
        on_event=lambda name, data: emit(name, **data),
        trace_dir=payload["trace_dir"],
    )
    provider = OpenAICompatibleProvider(config=config)
    llm.register_provider("openai", provider)
    llm.register_provider("openai-compatible", provider)
    ctx = SimpleNamespace(
        llm=llm,
        events=SimpleNamespace(emit=emit),
        meta=SimpleNamespace(render=lambda: payload["metadata"]),
        ui=SimpleNamespace(
            refresh=lambda reason: send({"type": "refresh", "reason": reason})
        ),
        logger=logging.getLogger("lecture_agent.notes.worker"),
    )
    agent = AgentService(ctx, "", 0, 200000, "")
    state = payload["state"]
    values = state["session"]
    session = agent.create_session(
        values["name"],
        workspace=restore_workspace(state["workspace"]),
        tools=state["tools"],
        max_tool_output=values["max_tool_output"],
    )
    agent._sessions.pop(session.id)
    for name, value in values.items():
        setattr(session, name, tuple(value) if name == "extra_instructions" else value)
    agent._sessions[session.id] = session
    # The parent resolves prompt contributors before this process starts.
    session.system_prompt = payload["system_prompt"]
    session.extra_instructions = ()
    try:
        result = agent.run(session.id, payload["instruction"])
        session.system_prompt = values["system_prompt"]
        session.extra_instructions = tuple(values["extra_instructions"])
        send(
            {
                "type": "result",
                "result": asdict(result),
                "state": session_state(session),
            }
        )
    finally:
        llm.close()


def main() -> None:
    def send(message):
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    try:
        run(json.load(sys.stdin), send)
    except Exception as exc:
        logging.exception("Note worker failed")
        send({"type": "error", "error": str(exc)})
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
