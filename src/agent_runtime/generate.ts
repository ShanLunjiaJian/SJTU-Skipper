import { streamSimple } from "@oh-my-pi/pi-ai";
import { setTimeout as delay } from "node:timers/promises";
import * as AIError from "@oh-my-pi/pi-ai/error";
import type {
  Model,
  Api,
  Message,
  ImageContent,
  TextContent,
  SimpleStreamOptions,
} from "@oh-my-pi/pi-ai";
import {
  AgentRole,
  ReasoningMode,
  RuntimeEvent,
  type Emit,
  generateInputSchema,
  validate,
} from "./schemas";

enum AttemptOutcome {
  THINKING_LOOP = "thinking_loop",
  ERROR = "error",
  SUCCESS = "success",
}

enum GenerationErrorCode {
  THINKING_LOOP = "thinking_loop",
}

export async function generate(
  model: Model<Api>,
  apiKey: SimpleStreamOptions["apiKey"],
  params: unknown,
  emit: Emit,
  signal: AbortSignal,
) {
  const input = validate(generateInputSchema, params, "generation input");
  const systemPrompts = input.system_prompt ? [input.system_prompt] : [];
  const messages: Message[] = [];
  for (const message of input.messages) {
    if (message.role === "system") {
      systemPrompts.push(
        typeof message.content === "string"
          ? message.content
          : message.content.map((part) => part.text).join("\n"),
      );
      continue;
    }
    const content: (TextContent | ImageContent)[] = [];
    if (typeof message.content === "string") content.push({ type: "text", text: message.content });
    else
      for (const part of message.content) {
        if (part.type === "text") content.push({ type: "text", text: part.text });
        else {
          const url = part.image_url.url;
          const separator = url.indexOf(",");
          content.push({
            type: "image",
            mimeType: url.slice(5, url.indexOf(";")),
            data: url.slice(separator + 1),
          });
        }
      }
    if (message.role === "assistant") {
      messages.push({
        role: "assistant",
        content,
        api: model.api,
        provider: model.provider,
        model: model.id,
        usage: {
          input: 0,
          output: 0,
          cacheRead: 0,
          cacheWrite: 0,
          totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
        },
        stopReason: "stop",
        timestamp: Date.now(),
      });
    } else messages.push({ role: message.role, content, timestamp: Date.now() });
  }
  const context = { systemPrompt: systemPrompts, messages };
  const options: SimpleStreamOptions = {
    apiKey,
    signal,
    maxTokens: input.max_tokens ?? undefined,
    temperature: input.temperature ?? undefined,
    reasoning:
      input.reasoning === ReasoningMode.OFF || input.reasoning === ReasoningMode.AUTO
        ? undefined
        : input.reasoning,
    forceReasoningOff: input.reasoning === ReasoningMode.OFF,
  };
  const board = (input.role ?? AgentRole.BOARD_RECOGNITION) === AgentRole.BOARD_RECOGNITION;
  let attempts = 0;
  let result;
  if (board) {
    const maxAttempts = input.loop_max_attempts ?? 3;
    for (;;) {
      signal.throwIfAborted();
      const events = streamSimple(model, context, options);
      for await (const _event of events) {
      }
      result = await events.result();
      ++attempts;
      const loop =
        result.stopReason === "error" && AIError.is(result.errorId, AIError.Flag.ThinkingLoop);
      emit(RuntimeEvent.LLM_ATTEMPT, {
        attempt: attempts,
        outcome: loop
          ? AttemptOutcome.THINKING_LOOP
          : result.stopReason === "error" || result.stopReason === "aborted"
            ? AttemptOutcome.ERROR
            : AttemptOutcome.SUCCESS,
        ...(result.errorMessage ? { error: result.errorMessage } : {}),
      });
      if (!loop || attempts >= maxAttempts) break;
      await delay(Math.min(500 * 2 ** (attempts - 1), 8000), undefined, { signal });
    }
    signal.throwIfAborted();
    if (result.stopReason === "error" && AIError.is(result.errorId, AIError.Flag.ThinkingLoop)) {
      return {
        content: "",
        model: result.model,
        provider: result.provider,
        finish_reason: result.stopReason,
        usage: result.usage,
        error: {
          code: GenerationErrorCode.THINKING_LOOP,
          message: `VLM transcription repeated after ${attempts} attempts`,
          attempts,
        },
      };
    }
  } else {
    const events = streamSimple(model, context, options);
    for await (const event of events) {
      if (event.type === "text_delta") emit(RuntimeEvent.LLM_DELTA, { text: event.delta });
      if (event.type === "thinking_delta") emit(RuntimeEvent.LLM_REASONING, { text: event.delta });
    }
    result = await events.result();
  }
  if (result.stopReason === "error" || result.stopReason === "aborted")
    throw new Error(result.errorMessage ?? result.stopReason);
  if (board && result.stopReason === "stop") {
    emit(RuntimeEvent.LLM_DELTA, {
      text: result.content
        .filter((p) => p.type === "text")
        .map((p) => p.text)
        .join("\n"),
    });
  }
  return {
    content: result.content
      .filter((p) => p.type === "text")
      .map((p) => p.text)
      .join("\n"),
    model: result.model,
    provider: result.provider,
    finish_reason: result.stopReason,
    usage: result.usage,
  };
}
