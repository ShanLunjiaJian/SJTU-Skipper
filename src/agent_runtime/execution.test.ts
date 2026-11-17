import { test, expect } from "bun:test";
import { ExecutionBlockKind, ExecutionBlockStatus, observeExecution } from "./execution";
import type { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";

test("streams reasoning and write arguments before execution", () => {
  let receive: (event: unknown) => void = () => {};
  const updates: {
    block?: {
      arguments?: { content?: string };
      status: ExecutionBlockStatus;
      kind: ExecutionBlockKind;
      text?: string;
    };
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
  expect(updates.at(-1)?.block?.status).toBe(ExecutionBlockStatus.STREAMING);
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
  expect(updates.at(-1)?.block?.status).toBe(ExecutionBlockStatus.RUNNING);
  receive({
    type: "tool_execution_end",
    toolCallId: "write-1",
    toolName: "write",
    result: { content: [{ type: "text", text: "Saved" }] },
  });
  expect(updates.at(-1)?.block?.status).toBe(ExecutionBlockStatus.COMPLETED);
  expect(updates.at(-1)?.metrics.context_tokens).toBe(32000);
  expect(
    updates.find((update) => update.block?.kind === ExecutionBlockKind.THINKING)?.block?.text,
  ).toContain("theorem");
  stop();
});

test("run block IDs remain distinct and interrupted blocks finish", () => {
  let receive: (event: unknown) => void = () => {};
  const updates: {
    block?: { id: string; kind: ExecutionBlockKind; status: ExecutionBlockStatus; text?: string };
  }[] = [];
  const session = {
    getContextUsage: () => undefined,
    subscribe: (callback: typeof receive) => {
      receive = callback;
      return () => {};
    },
  } as unknown as AgentSession;
  const start = () =>
    observeExecution(
      session,
      (_name, data) => updates.push(structuredClone(data) as unknown as (typeof updates)[number]),
      "writer",
    );
  const turn = () => {
    receive({
      type: "message_start",
      message: { role: "user", content: [{ type: "text", text: "User\nmessage" }] },
    });
    receive({ type: "message_start", message: { role: "assistant" } });
    receive({
      type: "message_update",
      assistantMessageEvent: {
        type: "text_delta",
        contentIndex: 0,
        partial: { content: [{ type: "text", text: "Partial" }] },
      },
    });
    receive({
      type: "message_end",
      message: { role: "assistant", stopReason: "aborted", usage: { output: 1 } },
    });
  };
  const first = start();
  turn();
  const firstId = updates.find((update) => update.block?.kind === ExecutionBlockKind.TEXT)!.block!
    .id;
  expect(updates.at(-2)?.block?.status).toBe(ExecutionBlockStatus.ABORTED);
  first();
  const second = start();
  turn();
  const textIds = new Set(
    updates
      .filter((update) => update.block?.kind === ExecutionBlockKind.TEXT)
      .map((update) => update.block!.id),
  );
  expect(textIds.size).toBe(2);
  expect(textIds.has(firstId)).toBe(true);
  expect(
    updates.find((update) => update.block?.kind === ExecutionBlockKind.USER)?.block?.text,
  ).toBe("User\nmessage");
  second();
});

test("final output IDs exclude preceding assistant messages", () => {
  let receive: (event: unknown) => void = () => {};
  const updates: { block?: { id: string; kind: ExecutionBlockKind } }[] = [];
  const session = {
    getContextUsage: () => undefined,
    subscribe: (callback: typeof receive) => {
      receive = callback;
      return () => {};
    },
  } as unknown as AgentSession;
  const stop = observeExecution(
    session,
    (_name, data) => updates.push(structuredClone(data) as unknown as (typeof updates)[number]),
    "reviewer",
  );
  const message = (parts: { type: string; text?: string; thinking?: string }[]) => {
    receive({ type: "message_start", message: { role: "assistant" } });
    for (const [index, part] of parts.entries()) {
      receive({
        type: "message_update",
        assistantMessageEvent: {
          type: `${part.type}_end`,
          contentIndex: index,
          partial: { content: parts },
        },
      });
    }
    receive({
      type: "message_end",
      message: { role: "assistant", stopReason: "stop", usage: { output: 1 } },
    });
  };
  message([{ type: "text", text: "Same text" }]);
  const commentaryId = stop.finalTextBlockIds()[0];
  message([
    { type: "thinking", thinking: "Check" },
    { type: "text", text: "Same text" },
    { type: "text", text: "Same text" },
  ]);
  const ids = stop.finalTextBlockIds();
  expect(ids).toHaveLength(2);
  expect(ids).not.toContain(commentaryId);
  expect(
    ids.every((id) =>
      updates.some(
        (update) => update.block?.id === id && update.block.kind === ExecutionBlockKind.TEXT,
      ),
    ),
  ).toBe(true);
  stop();
});
