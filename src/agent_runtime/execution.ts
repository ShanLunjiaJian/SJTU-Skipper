import type { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";

interface ExecutionBlock {
  id: string;
  kind: string;
  status: string;
  text?: string;
  name?: string;
  arguments?: unknown;
  result?: unknown;
  seconds?: number;
}

// Blocks retain provider content indexes and tool IDs across streaming updates.
export function observeExecution(
  session: AgentSession,
  emit: (name: string, data: Record<string, unknown>) => void,
  sessionId: string,
) {
  let turn = 0;
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
    emit("agent.execution", {
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
    if (event.type === "message_start" && event.message.role === "assistant") {
      ++turn;
      llmStarted = now;
      firstDelta = undefined;
    }
    if (event.type === "message_update") {
      const change = event.assistantMessageEvent;
      if (change.type.endsWith("_delta")) firstDelta ??= now;
      if (change.contentIndex !== undefined) {
        const part = change.partial.content[change.contentIndex];
        if (!part || (part.type !== "thinking" && part.type !== "text" && part.type !== "toolCall"))
          return;
        const id = part.type === "toolCall" ? part.id : `${turn}:${change.contentIndex}`;
        const previous = blocks.get(id);
        const block: ExecutionBlock = {
          id,
          kind: part.type === "toolCall" ? "tool" : part.type,
          status: change.type.endsWith("_end")
            ? part.type === "toolCall"
              ? "streaming"
              : "completed"
            : "streaming",
          ...(part.type === "thinking"
            ? { text: part.thinking }
            : part.type === "text"
              ? { text: part.text }
              : { name: part.name, arguments: part.arguments }),
          seconds: llmStarted === undefined ? 0 : (now - llmStarted) / 1000,
        };
        // Some providers allocate the tool id after the first argument fragment.
        if (previous?.status === "running") block.status = "running";
        blocks.set(id, block);
        publish(block);
      }
    }
    if (event.type === "message_end" && event.message.role === "assistant") {
      if (llmStarted !== undefined) llmSeconds += (now - llmStarted) / 1000;
      if (firstDelta !== undefined) generationSeconds += (now - firstDelta) / 1000;
      outputTokens += event.message.usage.output;
      llmStarted = undefined;
      publish();
    }
    if (event.type === "tool_execution_start") {
      toolStarts.set(event.toolCallId, now);
      const block = {
        id: event.toolCallId,
        kind: "tool",
        name: event.toolName,
        arguments: event.args,
        status: "running",
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
      const block = {
        ...blocks.get(event.toolCallId),
        id: event.toolCallId,
        kind: "tool",
        name: event.toolName,
        result: done ? event.result : event.partialResult,
        status: done ? (event.isError ? "failed" : "completed") : "running",
        seconds,
      };
      blocks.set(event.toolCallId, block);
      publish(block);
    }
  });
  const timer = setInterval(() => {
    if (llmStarted !== undefined || toolStarts.size) publish();
  }, 1000);
  return () => {
    clearInterval(timer);
    unsubscribe();
  };
}
