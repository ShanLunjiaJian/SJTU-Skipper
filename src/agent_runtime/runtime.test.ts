import { test, expect } from "bun:test";
import { mkdtemp, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { Runtime } from "./runtime";

test("first request creates missing runtime configuration", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "runtime-init-"));
  const root = path.join(directory, "missing");
  const runtime = new Runtime(root);
  try {
    await runtime.dispatch("ping", {}, () => {});
    expect((await stat(path.join(root, "models.yml"))).mode & 0o777).toBe(0o600);
  } finally {
    await runtime.close();
    await rm(directory, { recursive: true, force: true });
  }
});

test("generation honors disabled thinking", async () => {
  let body: Record<string, unknown> | undefined;
  const server = Bun.serve({
    port: 0,
    async fetch(request) {
      body = (await request.json()) as Record<string, unknown>;
      return new Response(
        'data: {"id":"proof","choices":[{"index":0,"delta":{"role":"assistant","content":"Board"},"finish_reason":null}]}\n\ndata: {"id":"proof","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
        { headers: { "Content-Type": "text/event-stream" } },
      );
    },
  });
  const root = await mkdtemp(path.join(tmpdir(), "runtime-thinking-"));
  const runtime = new Runtime(root);
  try {
    await runtime.dispatch(
      "configure",
      {
        models: JSON.stringify({
          providers: {
            deepseek: {
              api: "openai-completions",
              auth: "none",
              baseUrl: `http://127.0.0.1:${server.port}/v1`,
              models: [{ id: "deepseek-flash", reasoning: true, input: ["text", "image"] }],
            },
          },
        }),
        roles: "default: deepseek/deepseek-flash:off",
      },
      () => {},
    );
    const result = await runtime.dispatch(
      "generate",
      {
        messages: [{ role: "user", content: "Read board" }],
      },
      () => {},
    );
    expect(result).toHaveProperty("content", "Board");
    expect(body).toHaveProperty("thinking.type", "disabled");
  } finally {
    await runtime.close();
    server.stop(true);
    await rm(root, { recursive: true, force: true });
  }
});

test("cancel after generation cleanup is idempotent", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "runtime-cancel-"));
  const runtime = new Runtime(root);
  try {
    await expect(
      runtime.dispatch("cancel", { session_id: "completed" }, () => {}),
    ).resolves.toEqual({ ok: true });
  } finally {
    await runtime.close();
    await rm(root, { recursive: true, force: true });
  }
});
