import { streamSimple } from "@oh-my-pi/pi-ai";
import type {
  Model,
  Api,
  Message,
  ImageContent,
  TextContent,
  SimpleStreamOptions,
} from "@oh-my-pi/pi-ai";
import { generateInputSchema, validate } from "./schemas";

export async function generate(
  model: Model<Api>,
  apiKey: SimpleStreamOptions["apiKey"],
  params: unknown,
  emit: (name: string, data: Record<string, unknown>) => void,
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
  const events = streamSimple(
    model,
    { systemPrompt: systemPrompts, messages },
    {
      apiKey,
      signal,
      maxTokens: input.max_tokens ?? undefined,
      temperature: input.temperature ?? undefined,
      reasoning:
        input.reasoning === "off" || input.reasoning === "auto" ? undefined : input.reasoning,
      forceReasoningOff: input.reasoning === "off",
    },
  );
  for await (const event of events) {
    if (event.type === "text_delta") emit("llm.delta", { text: event.delta });
    if (event.type === "thinking_delta") emit("llm.reasoning", { text: event.delta });
  }
  const result = await events.result();
  if (result.stopReason === "error" || result.stopReason === "aborted")
    throw new Error(result.errorMessage ?? result.stopReason);
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
