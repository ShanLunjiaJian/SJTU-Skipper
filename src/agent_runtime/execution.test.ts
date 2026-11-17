import { test, expect } from "bun:test";
import { observeExecution } from "./execution";
import type { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";

test("streams reasoning and write arguments before execution", () => {
  let receive: (event: unknown) => void = () => {};
  const updates: {
    block?: { arguments?: { content?: string }; status: string; kind: string; text?: string };
    metrics: { context_tokens?: number };
  }[] = [];
  const session = {
    model: { id: "writer", contextWindow: 128000 },
    thinkingLevel: "high",
    getContextUsage: () => ({ tokens: 32000 }),
    subscribe: (callback: typeof receive) => {
      receive = callback;
      return () => {};
    },
  } as unknown as AgentSession;
  const stop = observeExecution(
    session,
    (_name, data) => updates.push(structuredClone(data) as unknown as (typeof updates)[number]),
    "writer",
  );
  receive({ type: "message_start", message: { role: "assistant" } });
  receive({
    type: "message_update",
    assistantMessageEvent: {
      type: "thinking_delta",
      contentIndex: 0,
      partial: { content: [{ type: "thinking", thinking: "Check the theorem conditions" }] },
    },
  });
  const part = {
    type: "toolCall",
    id: "write-1",
    name: "write",
    arguments: { path: "note.md", content: "# First" },
  };
  receive({
    type: "message_update",
    assistantMessageEvent: {
      type: "toolcall_delta",
      contentIndex: 1,
      partial: { content: [null, part] },
    },
  });
  expect(updates.at(-1)?.block?.arguments?.content).toBe("# First");
  expect(updates.at(-1)?.block?.status).toBe("streaming");
  part.arguments.content += "\nFull paragraph";
  receive({
    type: "message_update",
    assistantMessageEvent: {
      type: "toolcall_delta",
      contentIndex: 1,
      partial: { content: [null, part] },
    },
  });
  expect(updates.at(-1)?.block?.arguments?.content).toContain("Full paragraph");
  receive({
    type: "tool_execution_start",
    toolCallId: "write-1",
    toolName: "write",
    args: part.arguments,
  });
  expect(updates.at(-1)?.block?.status).toBe("running");
  receive({
    type: "tool_execution_end",
    toolCallId: "write-1",
    toolName: "write",
    result: { content: [{ type: "text", text: "Saved" }] },
  });
  expect(updates.at(-1)?.block?.status).toBe("completed");
  expect(updates.at(-1)?.metrics.context_tokens).toBe(32000);
  expect(updates.find((update) => update.block?.kind === "thinking")?.block?.text).toContain(
    "theorem",
  );
  stop();
});
