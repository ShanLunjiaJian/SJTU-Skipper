import { expect, test } from "bun:test";
import { normalizeModels } from "./discovery";

test("discovery maps OpenCode limits and modalities", () => {
  const models = normalizeModels({
    object: "list",
    data: [
      {
        id: "deepseek-v4.1-flash",
        name: "DeepSeek V4.1 Flash 1017",
        type: "chat",
        modalities: { input: ["text", "image/jpeg", "image/png"], output: ["text"] },
        capabilities: { reasoning: { supported: true } },
        limit: { context: 1_000_000, output: 384_000 },
      },
    ],
  });
  expect(models).toEqual([
    {
      id: "deepseek-v4.1-flash",
      name: "DeepSeek V4.1 Flash 1017",
      contextWindow: 1_000_000,
      maxTokens: 384_000,
      input: ["text", "image"],
      reasoning: true,
    },
  ]);
});

test("discovery falls back for bare OpenAI-compatible rows", () => {
  const models = normalizeModels({ data: [{ id: "plain" }, { id: "plain" }] });
  expect(models).toEqual([
    {
      id: "plain",
      contextWindow: 128_000,
      maxTokens: 8_192,
      input: ["text"],
      reasoning: false,
    },
  ]);
});

test("discovery reads OpenRouter-style context length and skips non-chat rows", () => {
  const models = normalizeModels({
    data: [
      {
        id: "vision",
        context_length: 200000,
        architecture: { input_modalities: ["text", "image"] },
      },
      { id: "embed", type: "embedding", context_length: 8192 },
      { not_a_model: true },
      "nonsense",
    ],
  });
  expect(models.map((model) => model.id)).toEqual(["vision"]);
  expect(models[0]?.contextWindow).toBe(200_000);
  expect(models[0]?.input).toEqual(["text", "image"]);
});

test("discovery tolerates malformed bodies", () => {
  expect(normalizeModels(null)).toEqual([]);
  expect(normalizeModels({ data: "nope" })).toEqual([]);
  expect(normalizeModels({})).toEqual([]);
});
