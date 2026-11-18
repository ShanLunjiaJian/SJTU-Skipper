import type { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import { RuntimeEvent, type Emit } from "./schemas";

export enum ExecutionBlockKind {
  USER = "user",
  THINKING = "thinking",
  TEXT = "text",
  TOOL = "tool",
}

export enum ExecutionBlockStatus {
  STREAMING = "streaming",
  RUNNING = "running",
  COMPLETED = "completed",
  CANCELLED = "cancelled",
  FAILED = "failed",
}

export interface ExecutionBlock {
  id: string;
  kind: ExecutionBlockKind;
  status: ExecutionBlockStatus;
  text?: string;
  name?: string;
  arguments?: unknown;
  result?: unknown;
  seconds?: number;
}

// Blocks retain provider content indexes and tool IDs across streaming updates.
export function observeExecution(session: AgentSession, emit: Emit, sessionId: string) {
  let turn = 0;
  const runId = crypto.randomUUID();
  let users = 0;
  const activeAssistant = new Set<string>();
  let llmStarted: number | undefined;
  let llmSeconds = 0;
  let outputTokens = 0;
  let generationSeconds = 0;
  let firstDelta: number | undefined;
  let toolSeconds = 0;
  const blocks = new Map<string, ExecutionBlock>();
  const toolStarts = new Map<string, number>();
  const publish = (block?: ExecutionBlock) => {
    const context = session.getContextUsage();
    emit(RuntimeEvent.EXECUTION, {
      session_id: sessionId,
      block,
      metrics: {
        model: session.model?.id,
        thinking: session.thinkingLevel ?? "off",
        context_tokens: context?.tokens,
        context_window: session.model?.contextWindow,
        llm_seconds:
          llmSeconds + (llmStarted === undefined ? 0 : (performance.now() - llmStarted) / 1000),
        tool_seconds:
          toolSeconds +
          [...toolStarts.values()].reduce(
            (total, start) => total + (performance.now() - start) / 1000,
            0,
          ),
        output_tokens: outputTokens,
        tokens_per_second: generationSeconds > 0 ? outputTokens / generationSeconds : undefined,
      },
    });
  };
  publish();
  const unsubscribe = session.subscribe((event) => {
    const now = performance.now();
    if (event.type === "message_start" && event.message.role === "user") {
      publish({
        id: `${runId}:user:${++users}`,
        kind: ExecutionBlockKind.USER,
        status: ExecutionBlockStatus.COMPLETED,
        text:
          typeof event.message.content === "string"
            ? event.message.content
            : event.message.content
                .filter((part) => part.type === "text")
                .map((part) => part.text)
                .join("\n"),
      });
    }
    if (event.type === "message_start" && event.message.role === "assistant") {
      ++turn;
      activeAssistant.clear();
      llmStarted = now;
      firstDelta = undefined;
    }
    if (event.type === "message_update") {
      const change = event.assistantMessageEvent;
      if (change.type.endsWith("_delta")) firstDelta ??= now;
      if ("partial" in change && change.contentIndex !== undefined) {
        const part = change.partial.content[change.contentIndex];
        if (!part || (part.type !== "thinking" && part.type !== "text" && part.type !== "toolCall"))
          return;
        const id = part.type === "toolCall" ? part.id : `${runId}:${turn}:${change.contentIndex}`;
        activeAssistant.add(id);
        const previous = blocks.get(id);
        const block: ExecutionBlock = {
          id,
          kind:
            part.type === "toolCall"
              ? ExecutionBlockKind.TOOL
              : part.type === "thinking"
                ? ExecutionBlockKind.THINKING
                : ExecutionBlockKind.TEXT,
          status: change.type.endsWith("_end")
            ? part.type === "toolCall"
              ? ExecutionBlockStatus.STREAMING
              : ExecutionBlockStatus.COMPLETED
            : ExecutionBlockStatus.STREAMING,
          ...(part.type === "thinking"
            ? { text: part.thinking }
            : part.type === "text"
              ? { text: part.text }
              : { name: part.name, arguments: part.arguments }),
          seconds: llmStarted === undefined ? 0 : (now - llmStarted) / 1000,
        };
        // Some providers allocate the tool id after the first argument fragment.
        if (previous?.status === ExecutionBlockStatus.RUNNING)
          block.status = ExecutionBlockStatus.RUNNING;
        blocks.set(id, block);
        publish(block);
      }
    }
    if (event.type === "message_end" && event.message.role === "assistant") {
      if (llmStarted !== undefined) llmSeconds += (now - llmStarted) / 1000;
      if (firstDelta !== undefined) generationSeconds += (now - firstDelta) / 1000;
      outputTokens += event.message.usage.output;
      llmStarted = undefined;
      for (const id of activeAssistant) {
        const block = blocks.get(id);
        if (block?.status !== ExecutionBlockStatus.STREAMING) continue;
        block.status =
          event.message.stopReason === "aborted"
            ? ExecutionBlockStatus.CANCELLED
            : event.message.stopReason === "error"
              ? ExecutionBlockStatus.FAILED
              : block.kind === ExecutionBlockKind.TOOL
                ? ExecutionBlockStatus.STREAMING
                : ExecutionBlockStatus.COMPLETED;
        publish(block);
      }
      publish();
    }
    if (event.type === "tool_execution_start") {
      toolStarts.set(event.toolCallId, now);
      const block: ExecutionBlock = {
        id: event.toolCallId,
        kind: ExecutionBlockKind.TOOL,
        name: event.toolName,
        arguments: event.args,
        status: ExecutionBlockStatus.RUNNING,
      };
      blocks.set(event.toolCallId, block);
      publish(block);
    }
    if (event.type === "tool_execution_update" || event.type === "tool_execution_end") {
      const start = toolStarts.get(event.toolCallId);
      const seconds = start === undefined ? 0 : (now - start) / 1000;
      const done = event.type === "tool_execution_end";
      if (done) {
        toolSeconds += seconds;
        toolStarts.delete(event.toolCallId);
      }
      const block: ExecutionBlock = {
        ...blocks.get(event.toolCallId),
        id: event.toolCallId,
        kind: ExecutionBlockKind.TOOL,
        name: event.toolName,
        result: done ? event.result : event.partialResult,
        status: done
          ? event.isError
            ? ExecutionBlockStatus.FAILED
            : ExecutionBlockStatus.COMPLETED
          : ExecutionBlockStatus.RUNNING,
        seconds,
      };
      blocks.set(event.toolCallId, block);
      publish(block);
    }
    if (event.type === "agent_end") {
      for (const block of blocks.values()) {
        if (
          block.status !== ExecutionBlockStatus.STREAMING &&
          block.status !== ExecutionBlockStatus.RUNNING
        )
          continue;
        block.status = ExecutionBlockStatus.CANCELLED;
        publish(block);
      }
    }
  });
  const timer = setInterval(() => {
    if (llmStarted !== undefined || toolStarts.size) publish();
  }, 1000);
  return Object.assign(
    () => {
      clearInterval(timer);
      unsubscribe();
    },
    {
      finalTextBlockIds: () =>
        [...activeAssistant].filter((id) => blocks.get(id)?.kind === ExecutionBlockKind.TEXT),
    },
  );
}
