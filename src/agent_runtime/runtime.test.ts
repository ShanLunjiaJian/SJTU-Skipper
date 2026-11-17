import { test, expect, spyOn } from "bun:test";
import { mkdtemp, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { wrapSteeringForModel } from "@oh-my-pi/pi-coding-agent/session/messages";
import { Runtime } from "./runtime";
import { ReviewStatus } from "./review";
import {
  AgentRole,
  IpcMethod,
  RuntimeEvent,
  WorkspaceTool,
  ipcRequestSchema,
  validate,
} from "./schemas";

test("first request creates missing runtime configuration", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "runtime-init-"));
  const root = path.join(directory, "missing");
  const runtime = new Runtime(root);
  try {
    await runtime.dispatch(IpcMethod.PING, {}, () => {});
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
      IpcMethod.CONFIGURE,
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
      IpcMethod.GENERATE,
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
      runtime.dispatch(IpcMethod.CANCEL, { session_id: "completed" }, () => {}),
    ).resolves.toEqual({ ok: true });
  } finally {
    await runtime.close();
    await rm(root, { recursive: true, force: true });
  }
});

interface ModelRequest {
  messages: { role: string; content?: unknown }[];
}
function userTexts(request: ModelRequest) {
  return request.messages
    .filter((message) => message.role === "user")
    .map((message) => {
      if (typeof message.content === "string") return message.content;
      return (message.content as { type: string; text?: string }[])
        .filter((block) => block.type === "text")
        .map((block) => block.text ?? "")
        .join("");
    });
}
function taskWithoutReminder(text: string) {
  return text.replace(/^<system-reminder>\nToday: [^\n]+\n<\/system-reminder>/, "");
}
function nativeSteeringText(text: string) {
  const [message] = wrapSteeringForModel([
    { role: "user", content: text, steering: true, attribution: "user", timestamp: 0 },
  ]);
  if (message?.role !== "user" || typeof message.content !== "string")
    throw new Error("Native steering did not return user text");
  return message.content;
}
function modelText(content: string) {
  return modelDelta({ content }, "stop");
}
function modelDelta(delta: Record<string, unknown>, reason: string) {
  return `data: ${JSON.stringify({ id: "proof", choices: [{ index: 0, delta: { role: "assistant", ...delta }, finish_reason: null }] })}\n\ndata: ${JSON.stringify({ id: "proof", choices: [{ index: 0, delta: {}, finish_reason: reason }] })}\n\ndata: [DONE]\n\n`;
}
function modelTool(name: string, args: Record<string, unknown>, id: string) {
  return modelDelta(
    {
      tool_calls: [
        { index: 0, id, type: "function", function: { name, arguments: JSON.stringify(args) } },
      ],
    },
    "tool_calls",
  );
}
function sse(text: string) {
  return new Response(text, { headers: { "Content-Type": "text/event-stream" } });
}
async function localRuntime(respond: (body: ModelRequest) => Response | Promise<Response>) {
  const server = Bun.serve({
    port: 0,
    async fetch(request) {
      return respond((await request.json()) as ModelRequest);
    },
  });
  const root = await mkdtemp(path.join(tmpdir(), "runtime-live-"));
  const runtime = new Runtime(root);
  await runtime.dispatch(
    IpcMethod.CONFIGURE,
    {
      models: JSON.stringify({
        providers: {
          local: {
            api: "openai-completions",
            auth: "none",
            baseUrl: `http://127.0.0.1:${server.port}/v1`,
            models: [
              {
                id: "proof",
                reasoning: false,
                compat: { supportsDeveloperRole: false },
                input: ["text", "image"],
                contextWindow: 128000,
                maxTokens: 4096,
              },
            ],
          },
        },
      }),
      roles: "default: local/proof:off",
    },
    () => {},
  );
  return {
    runtime,
    root,
    async close() {
      await runtime.close();
      server.stop(true);
      await rm(root, { recursive: true, force: true });
    },
  };
}

test("configured instructions remain ordered user messages", async () => {
  const requests: ModelRequest[] = [];
  const fixture = await localRuntime((body) => {
    requests.push(body);
    return sse(modelText("Done without review"));
  });
  try {
    await fixture.runtime.dispatch(
      IpcMethod.CREATE_SESSION,
      {
        id: "role-prompt",
        prompt: "CONFIGURABLE_ROLE_INSTRUCTIONS",
      },
      () => {},
    );
    const result = await fixture.runtime.dispatch(
      IpcMethod.RUN,
      {
        session_id: "role-prompt",
        instruction: "/literal task",
        follow_ups: ["first follow-up", "second follow-up"],
      },
      () => {},
    );
    expect(requests).toHaveLength(2);
    expect(userTexts(requests[0]!).map(taskWithoutReminder)).toEqual([
      "CONFIGURABLE_ROLE_INSTRUCTIONS\n\n/literal task",
    ]);
    expect(userTexts(requests[1]!).map(taskWithoutReminder)).toEqual([
      "CONFIGURABLE_ROLE_INSTRUCTIONS\n\n/literal task",
      "first follow-up",
      "second follow-up",
    ]);
    expect(
      requests[0]!.messages
        .filter((message) => message.role === "system")
        .some((message) =>
          JSON.stringify(message.content).includes("CONFIGURABLE_ROLE_INSTRUCTIONS"),
        ),
    ).toBe(false);
    expect(result).toHaveProperty("final_message", "Done without review");
    expect(result).toHaveProperty("review_status", ReviewStatus.PASSED);
  } finally {
    await fixture.close();
  }
});

test("writer cannot complete before a passing review", async () => {
  let writerCalls = 0;
  let reviewerCalls = 0;
  const fixture = await localRuntime((body) => {
    if (JSON.stringify(body.messages).includes("REQUIRED_REVIEW")) {
      ++reviewerCalls;
      return sse(
        modelText(
          JSON.stringify({
            correct: true,
            explanation: "Checked",
            confidence: 1,
            findings: [],
          }),
        ),
      );
    }
    ++writerCalls;
    if (writerCalls === 1) return sse(modelText("Premature completion"));
    if (writerCalls === 2) {
      expect(userTexts(body).at(-1)).toContain("do not have a passing review");
      return sse(modelTool("review_notes", {}, "required-review"));
    }
    return sse(modelText("Reviewed completion"));
  });
  try {
    const files: Record<string, string> = {};
    for (const name of [
      "lecture_source.md",
      "note_knowledge.md",
      "note_gossip.md",
      "note_notice.md",
    ]) {
      files[name] = path.join(fixture.root, name);
      await Bun.write(files[name]!, "Notes");
    }
    await fixture.runtime.dispatch(
      IpcMethod.CREATE_SESSION,
      {
        id: "required",
        files,
        meta: { review: { system: "REQUIRED_REVIEW", prompt: "Review", maxRounds: 1 } },
      },
      () => {},
    );
    const result = await fixture.runtime.dispatch(
      IpcMethod.RUN,
      {
        session_id: "required",
        instruction: "Write notes",
      },
      () => {},
    );
    expect(result).toHaveProperty("final_message", "Reviewed completion");
    expect(result).toHaveProperty("review_status", ReviewStatus.PASSED);
    expect(reviewerCalls).toBe(1);
  } finally {
    await fixture.close();
  }
});

test("an accepted review survives a harmless user message", async () => {
  const writerStarted = Promise.withResolvers<void>();
  let reviews = 0;
  let writers = 0;
  const fixture = await localRuntime((body) => {
    if (JSON.stringify(body.messages).includes("ACCEPTED_REVIEW")) {
      ++reviews;
      return sse(
        modelText(
          JSON.stringify({
            correct: true,
            explanation: "Checked",
            confidence: 1,
            findings: [],
          }),
        ),
      );
    }
    ++writers;
    if (writers === 1) return sse(modelTool("review_notes", {}, "accepted-review"));
    if (writers === 2)
      return new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(
              new TextEncoder().encode(
                'data: {"id":"proof","choices":[{"index":0,"delta":{"role":"assistant","content":"Finishing"},"finish_reason":null}]}\n\n',
              ),
            );
            writerStarted.resolve();
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      );
    expect(userTexts(body).at(-1)).toBe(nativeSteeringText("Keep the accepted notes unchanged"));
    return sse(modelText("Finished"));
  });
  try {
    const files: Record<string, string> = {};
    for (const name of [
      "lecture_source.md",
      "note_knowledge.md",
      "note_gossip.md",
      "note_notice.md",
    ]) {
      files[name] = path.join(fixture.root, name);
      await Bun.write(files[name]!, "Notes");
    }
    await fixture.runtime.dispatch(
      IpcMethod.CREATE_SESSION,
      {
        id: "accepted",
        files,
        meta: { review: { system: "ACCEPTED_REVIEW", prompt: "Review", maxRounds: 1 } },
      },
      () => {},
    );
    const run = fixture.runtime.dispatch(
      IpcMethod.RUN,
      {
        session_id: "accepted",
        instruction: "Review notes",
      },
      () => {},
    );
    await writerStarted.promise;
    expect(reviews).toBe(1);
    expect(
      await fixture.runtime.dispatch(
        IpcMethod.MESSAGE,
        {
          session_id: "accepted",
          message: "Keep the accepted notes unchanged",
        },
        () => {},
      ),
    ).toEqual({ accepted: true });
    expect(await run).toMatchObject({ final_message: "Finished", review_status: "passed" });
    expect(reviews).toBe(1);
    expect(writers).toBe(3);
  } finally {
    await fixture.close();
  }
});

for (const exhausted of [false, true]) {
  test(`board loop ${exhausted ? "exhaustion" : "recovery"} keeps failed text private`, async () => {
    let requests = 0;
    const loop = String.raw`\lambda_1 = \frac{1-\lambda\beta}{1-\lambda}\quad \rho=\lambda_1-\gamma^2`;
    const env = await localRuntime(() => {
      ++requests;
      return sse(modelText(exhausted || requests === 1 ? loop.repeat(40) : "Complete board"));
    });
    const events: { name: RuntimeEvent; data: Record<string, unknown> }[] = [];
    try {
      const result = await env.runtime.dispatch(
        IpcMethod.GENERATE,
        {
          messages: [{ role: "user", content: "Read board" }],
        },
        (name, data) => events.push({ name, data }),
      );
      expect(requests).toBe(exhausted ? 3 : 2);
      expect(result).toHaveProperty("content", exhausted ? "" : "Complete board");
      if (exhausted) expect(result).toHaveProperty("error.code", "thinking_loop");
      expect(
        events
          .filter((event) => event.name === RuntimeEvent.LLM_DELTA)
          .map((event) => event.data.text),
      ).toEqual(exhausted ? [] : ["Complete board"]);
    } finally {
      await env.close();
    }
  });
}

test("cancel during loop backoff prevents another attempt", async () => {
  let requests = 0;
  const env = await localRuntime(() => {
    ++requests;
    return sse(modelText("abcdefghijklmno1234567890".repeat(100)));
  });
  try {
    const result = env.runtime.dispatch(
      IpcMethod.GENERATE,
      {
        session_id: "cancel-loop",
        messages: [{ role: "user", content: "Read board" }],
      },
      (name) => {
        if (name === RuntimeEvent.LLM_ATTEMPT)
          void env.runtime.dispatch(IpcMethod.CANCEL, { session_id: "cancel-loop" }, () => {});
      },
    );
    await expect(result).rejects.toThrow();
    expect(requests).toBe(1);
  } finally {
    await env.close();
  }
});

test("ordinary provider errors do not become loop retries", async () => {
  let requests = 0;
  const events: { name: RuntimeEvent; data: Record<string, unknown> }[] = [];
  const env = await localRuntime(() => {
    ++requests;
    return new Response(
      JSON.stringify({
        error: { message: "Invalid board request", type: "invalid_request_error" },
      }),
      {
        status: 400,
        headers: { "Content-Type": "application/json" },
      },
    );
  });
  try {
    await expect(
      env.runtime.dispatch(
        IpcMethod.GENERATE,
        {
          messages: [{ role: "user", content: "Read board" }],
        },
        (name, data) => events.push({ name, data }),
      ),
    ).rejects.toThrow("Invalid board request");
    expect(requests).toBe(1);
    expect(events.filter((event) => event.name === RuntimeEvent.LLM_DELTA)).toEqual([]);
    expect(
      events
        .filter((event) => event.name === RuntimeEvent.LLM_ATTEMPT)
        .map((event) => event.data.outcome),
    ).toEqual(["error"]);
  } finally {
    await env.close();
  }
});

test("non-board generation preserves streamed deltas", async () => {
  const encoder = new TextEncoder();
  const first = Promise.withResolvers<void>();
  const release = Promise.withResolvers<void>();
  const env = await localRuntime(
    () =>
      new Response(
        new ReadableStream({
          async start(controller) {
            controller.enqueue(
              encoder.encode(
                'data: {"id":"proof","choices":[{"index":0,"delta":{"role":"assistant","content":"First "},"finish_reason":null}]}\n\n',
              ),
            );
            await release.promise;
            controller.enqueue(encoder.encode(modelText("second")));
            controller.close();
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      ),
  );
  const deltas: unknown[] = [];
  const attempts: unknown[] = [];
  try {
    const result = env.runtime.dispatch(
      IpcMethod.GENERATE,
      {
        role: AgentRole.NOTE_WRITER,
        messages: [{ role: "user", content: "Write notes" }],
      },
      (name, data) => {
        if (name === RuntimeEvent.LLM_DELTA) {
          deltas.push(data.text);
          first.resolve();
        }
        if (name === RuntimeEvent.LLM_ATTEMPT) attempts.push(data);
      },
    );
    await first.promise;
    expect(deltas).toEqual(["First "]);
    release.resolve();
    expect(await result).toHaveProperty("content", "First second");
    expect(deltas).toEqual(["First ", "second"]);
    expect(attempts).toEqual([]);
  } finally {
    release.resolve();
    await env.close();
  }
});

test("cancel interrupts an active board stream", async () => {
  let requests = 0;
  const started = Promise.withResolvers<void>();
  const originalFetch = globalThis.fetch;
  const fetchSpy = spyOn(globalThis, "fetch").mockImplementation(
    Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const response = await originalFetch(...args);
        started.resolve();
        return response;
      },
      { preconnect: originalFetch.preconnect },
    ),
  );
  let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
  const env = await localRuntime(() => {
    ++requests;
    return new Response(
      new ReadableStream({
        start(controller) {
          streamController = controller;
          controller.enqueue(
            new TextEncoder().encode(
              'data: {"id":"proof","choices":[{"index":0,"delta":{"role":"assistant","content":"Partial board"},"finish_reason":null}]}\n\n',
            ),
          );
        },
      }),
      { headers: { "Content-Type": "text/event-stream" } },
    );
  });
  const deltas: unknown[] = [];
  try {
    const result = env.runtime.dispatch(
      IpcMethod.GENERATE,
      {
        session_id: "active-board",
        messages: [{ role: "user", content: "Read board" }],
      },
      (name, data) => {
        if (name === RuntimeEvent.LLM_DELTA) deltas.push(data.text);
      },
    );
    const rejected = expect(result).rejects.toThrow();
    await started.promise;
    await env.runtime.dispatch(IpcMethod.CANCEL, { session_id: "active-board" }, () => {});
    await rejected;
    expect(requests).toBe(1);
    expect(deltas).toEqual([]);
  } finally {
    fetchSpy.mockRestore();
    try {
      streamController?.close();
    } catch {
      /* The request can already be cancelled. */
    }
    await env.close();
  }
}, 15000);

test("messages interrupt a stream within one run", async () => {
  const started = Promise.withResolvers<void>();
  const disconnected = Promise.withResolvers<void>();
  const requests: ModelRequest[] = [];
  const fixture = await localRuntime((body) => {
    requests.push(body);
    if (requests.length > 1) return sse(modelText("Finished"));
    return new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(
            new TextEncoder().encode(
              'data: {"id":"proof","choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":"Thinking"},"finish_reason":null}]}\n\n',
            ),
          );
          started.resolve();
        },
        cancel() {
          disconnected.resolve();
        },
      }),
      { headers: { "Content-Type": "text/event-stream" } },
    );
  });
  try {
    const { runtime } = fixture;
    await runtime.dispatch(IpcMethod.CREATE_SESSION, { id: "live" }, () => {});
    const conversation = runtime["sessions"].get("live")!;
    const join = spyOn(conversation.session, "joinActiveTurn");
    const abort = spyOn(conversation.session, "abort");
    expect(
      await runtime.dispatch(
        IpcMethod.MESSAGE,
        { session_id: "live", message: "before run" },
        () => {},
      ),
    ).toHaveProperty("accepted", false);
    const events: string[] = [];
    const run = runtime.dispatch(
      IpcMethod.RUN,
      { session_id: "live", instruction: "Start" },
      (name) => events.push(name),
    );
    await started.promise;
    await expect(
      runtime.dispatch(IpcMethod.RUN, { session_id: "live", instruction: "Duplicate" }, () => {}),
    ).rejects.toThrow("active run");
    const first = runtime.dispatch(
      IpcMethod.MESSAGE,
      { session_id: "live", message: "/literal\nfirst" },
      () => {},
    );
    const second = runtime.dispatch(
      IpcMethod.MESSAGE,
      { session_id: "live", message: "second" },
      () => {},
    );
    expect(await first).toEqual({ accepted: true });
    expect(await second).toEqual({ accepted: true });
    expect(await run).toHaveProperty("final_message", "Finished");
    await disconnected.promise;
    expect(userTexts(requests.at(-1)!).map(taskWithoutReminder)).toEqual([
      "Start",
      nativeSteeringText("/literal\nfirst"),
      nativeSteeringText("second"),
    ]);
    expect(join.mock.calls).toEqual([
      ["/literal\nfirst", undefined, { queue: "steering" }],
      ["second", undefined, { queue: "steering" }],
    ]);
    expect(await Promise.all(join.mock.results.map((result) => result.value))).toEqual([
      true,
      true,
    ]);
    expect(abort).toHaveBeenCalledTimes(1);
    expect(
      conversation.session.messages.filter((message) => message.role === "user"),
    ).toMatchObject([
      { role: "user", content: [{ type: "text", text: "Start" }], attribution: "user" },
      {
        role: "user",
        content: [{ type: "text", text: "/literal\nfirst" }],
        attribution: "user",
        steering: true,
      },
      {
        role: "user",
        content: [{ type: "text", text: "second" }],
        attribution: "user",
        steering: true,
      },
    ]);
    expect(conversation.run).toBeUndefined();
    expect(conversation.session.agent.hasQueuedMessages()).toBe(false);
    expect(events.filter((name) => name === RuntimeEvent.RUN_READY)).toHaveLength(1);
    expect(events.filter((name) => name === RuntimeEvent.MESSAGE)).toHaveLength(1);
    expect(
      await runtime.dispatch(IpcMethod.MESSAGE, { session_id: "live", message: "late" }, () => {}),
    ).toHaveProperty("accepted", false);
  } finally {
    await fixture.close();
  }
});

test("cancel rejects messages without continuation", async () => {
  const started = Promise.withResolvers<void>();
  let calls = 0;
  const fixture = await localRuntime(() => {
    ++calls;
    return new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(
            new TextEncoder().encode(
              'data: {"id":"proof","choices":[{"index":0,"delta":{"role":"assistant","content":"Working"},"finish_reason":null}]}\n\n',
            ),
          );
        },
      }),
      { headers: { "Content-Type": "text/event-stream" } },
    );
  });
  try {
    await fixture.runtime.dispatch(IpcMethod.CREATE_SESSION, { id: "cancel" }, () => {});
    const run = fixture.runtime.dispatch(
      IpcMethod.RUN,
      { session_id: "cancel", instruction: "Start" },
      (name) => {
        if (name === RuntimeEvent.DELTA) started.resolve();
      },
    );
    const outcome = run.then(
      () => undefined,
      (error: unknown) => error,
    );
    await started.promise;
    const cancel = fixture.runtime.dispatch(IpcMethod.CANCEL, { session_id: "cancel" }, () => {});
    expect(
      await fixture.runtime.dispatch(
        IpcMethod.MESSAGE,
        { session_id: "cancel", message: "Continue" },
        () => {},
      ),
    ).toHaveProperty("accepted", false);
    await cancel;
    expect(String(await outcome)).toContain("cancelled");
    expect(calls).toBe(1);
    await fixture.runtime.dispatch(IpcMethod.CLOSE_SESSION, { session_id: "cancel" }, () => {});
    await fixture.runtime.dispatch(IpcMethod.CLOSE_SESSION, { session_id: "cancel" }, () => {});
  } finally {
    await fixture.close();
  }
});

test("reopen requires readable history and preserves its messages", async () => {
  const requests: ModelRequest[] = [];
  const fixture = await localRuntime((body) => {
    requests.push(body);
    return sse(modelText("Saved answer"));
  });
  try {
    const { runtime, root } = fixture;
    await expect(
      runtime.dispatch(IpcMethod.REOPEN_SESSION, { id: "missing" }, () => {}),
    ).rejects.toThrow("unavailable");
    await Bun.write(path.join(root, "session-broken.path"), path.join(root, "broken.jsonl"));
    await Bun.write(path.join(root, "broken.jsonl"), "not json");
    await expect(
      runtime.dispatch(IpcMethod.REOPEN_SESSION, { id: "broken" }, () => {}),
    ).rejects.toThrow("invalid");
    await runtime.dispatch(IpcMethod.CREATE_SESSION, { id: "history" }, () => {});
    await runtime.dispatch(
      IpcMethod.RUN,
      { session_id: "history", instruction: "Original task" },
      () => {},
    );
    await expect(
      runtime.dispatch(IpcMethod.REOPEN_SESSION, { id: "history" }, () => {}),
    ).rejects.toThrow("already open");
    await runtime.dispatch(IpcMethod.CLOSE_SESSION, { session_id: "history" }, () => {});
    await runtime.dispatch(IpcMethod.REOPEN_SESSION, { id: "history" }, () => {});
    await runtime.dispatch(
      IpcMethod.RUN,
      { session_id: "history", instruction: "Continue task" },
      () => {},
    );
    expect(JSON.stringify(requests.at(-1)?.messages)).toContain("Original task");
    expect(JSON.stringify(requests.at(-1)?.messages)).toContain("Saved answer");
  } finally {
    await fixture.close();
  }
});

test("writer messages preserve acceptance for unchanged files", async () => {
  const reviewerStarted = Promise.withResolvers<void>();
  const correctionStarted = Promise.withResolvers<void>();
  const reviewerRelease = Promise.withResolvers<void>();
  let writerCalls = 0;
  let reviewCalls = 0;
  const requests: ModelRequest[] = [];
  const fixture = await localRuntime(async (body) => {
    requests.push(body);
    if (JSON.stringify(body.messages).includes("REVIEW_PROOF")) {
      if (++reviewCalls === 1) {
        reviewerStarted.resolve();
        await reviewerRelease.promise;
      }
      return sse(
        modelText(
          JSON.stringify({
            correct: true,
            explanation: "Checked",
            confidence: 1,
            findings: [],
          }),
        ),
      );
    }
    ++writerCalls;
    if (writerCalls === 1) return sse(modelTool("review_notes", {}, "review-1"));
    if (writerCalls === 2) correctionStarted.resolve();
    return sse(modelText(writerCalls === 2 ? "Correction applied" : "Final answer"));
  });
  try {
    const files: Record<string, string> = {};
    for (const name of [
      "lecture_source.md",
      "note_knowledge.md",
      "note_gossip.md",
      "note_notice.md",
    ]) {
      files[name] = path.join(fixture.root, name);
      await Bun.write(files[name]!, "Notes");
    }
    const events: { name: RuntimeEvent; data: Record<string, unknown> }[] = [];
    await fixture.runtime.dispatch(
      IpcMethod.CREATE_SESSION,
      {
        id: "writer",
        files,
        meta: {
          review: {
            system: "REVIEW_PROOF",
            prompt: "Review independently",
            maxRounds: 3,
          },
        },
      },
      () => {},
    );
    const run = fixture.runtime.dispatch(
      IpcMethod.RUN,
      { session_id: "writer", instruction: "Review notes" },
      (name, data) => events.push({ name, data }),
    );
    await reviewerStarted.promise;
    expect(
      await fixture.runtime.dispatch(
        IpcMethod.MESSAGE,
        { session_id: "writer", message: "Keep the current notes" },
        () => {},
      ),
    ).toEqual({ accepted: true });
    await correctionStarted.promise;
    expect(reviewCalls).toBe(1);
    reviewerRelease.resolve();
    const result = await run;
    expect(result).toHaveProperty("final_message", "Final answer");
    expect(result).toHaveProperty("review_status", ReviewStatus.PASSED);
    expect(reviewCalls).toBe(1);
    const finished = events.filter(
      ({ name, data }) =>
        name === RuntimeEvent.REVIEWER && data.event === RuntimeEvent.REVIEW_FINISHED,
    );
    expect(finished.map(({ data }) => data.stale)).toEqual([false]);
    const sourceIds = finished[0]!.data.block_ids as string[];
    expect(sourceIds.length).toBeGreaterThan(0);
    for (const id of sourceIds) {
      expect(
        events.some(({ name, data }) => {
          const block = data.block as { id?: string; kind?: string } | undefined;
          return (
            name === RuntimeEvent.REVIEWER &&
            data.event === RuntimeEvent.EXECUTION &&
            block?.id === id &&
            block.kind === "text"
          );
        }),
      ).toBe(true);
    }
    const deliveryPrompts = requests.flatMap((request) =>
      request.messages.filter(
        (message) =>
          message.role !== "assistant" &&
          message.role !== "tool" &&
          JSON.stringify(message.content).includes("correct"),
      ),
    );
    const unique = new Set(deliveryPrompts.map((message) => JSON.stringify(message.content)));
    expect(unique.size).toBe(1);
    expect(deliveryPrompts.length).toBeGreaterThan(0);
    const native = fixture.runtime["sessions"].get("writer")!.session.messages;
    const deliveries = native.filter(
      (message) =>
        message.role === "developer" && JSON.stringify(message.content).includes("correct"),
    );
    expect(deliveries).toHaveLength(1);
    expect(deliveries[0]).toMatchObject({
      role: "developer",
      synthetic: true,
      attribution: "agent",
    });
    expect(
      native
        .filter((message) => message.role === "user")
        .some((message) => JSON.stringify(message.content).includes("correct")),
    ).toBe(false);
    expect(deliveryPrompts.every((message) => message.role === "user")).toBe(true);
  } finally {
    reviewerRelease.resolve();
    await fixture.close();
  }
});

test("a new prompt waits for an interrupted write to finish", async () => {
  const writing = Promise.withResolvers<void>();
  const release = Promise.withResolvers<void>();
  let calls = 0;
  let safe = false;
  let published = false;
  const fixture = await localRuntime(() => {
    ++calls;
    if (calls === 1)
      return sse(modelTool("write", { path: "note.md", content: "Changed" }, "write-proof"));
    expect(safe).toBe(true);
    expect(published).toBe(true);
    return sse(modelText("Saved safely"));
  });
  try {
    const source = path.join(fixture.root, "note.md");
    await Bun.write(source, "Original");
    await fixture.runtime.dispatch(
      IpcMethod.CREATE_SESSION,
      { id: "write", tools: [WorkspaceTool.WRITE], files: { "note.md": source } },
      () => {},
    );
    const conversation = fixture.runtime["sessions"].get("write")!;
    const tool = conversation.session.agent.state.tools.find(
      (candidate) => candidate.name === "write",
    )!;
    const execute = tool.execute.bind(tool);
    tool.execute = async (...args) => {
      const result = await execute(...args);
      writing.resolve();
      await release.promise;
      safe = true;
      return result;
    };
    const run = fixture.runtime.dispatch(
      IpcMethod.RUN,
      { session_id: "write", instruction: "Write the note" },
      (name) => {
        if (name === RuntimeEvent.FILE) published = true;
      },
    );
    await writing.promise;
    expect(
      await fixture.runtime.dispatch(
        IpcMethod.MESSAGE,
        { session_id: "write", message: "Continue after saving" },
        () => {},
      ),
    ).toEqual({ accepted: true });
    expect(calls).toBe(1);
    release.resolve();
    expect(await run).toHaveProperty("final_message", "Saved safely");
    expect(calls).toBe(2);
  } finally {
    release.resolve();
    await fixture.close();
  }
});

test("completion accepts and runs a boundary message or rejects it", async () => {
  let calls = 0;
  const fixture = await localRuntime(() => sse(modelText(`Answer ${++calls}`)));
  try {
    await fixture.runtime.dispatch(IpcMethod.CREATE_SESSION, { id: "boundary" }, () => {});
    const conversation = fixture.runtime["sessions"].get("boundary")!;
    const workspace = conversation.workspace;
    const join = spyOn(conversation.session, "joinActiveTurn");
    const changes = workspace.changes.bind(workspace);
    let injected = false;
    workspace.changes = async () => {
      const result = await changes();
      if (!injected) {
        injected = true;
        expect(
          await fixture.runtime.dispatch(
            IpcMethod.MESSAGE,
            { session_id: "boundary", message: "At completion" },
            () => {},
          ),
        ).toEqual({ accepted: true });
      }
      return result;
    };
    expect(
      await fixture.runtime.dispatch(
        IpcMethod.RUN,
        { session_id: "boundary", instruction: "Start" },
        () => {},
      ),
    ).toHaveProperty("final_message", "Answer 2");
    expect(calls).toBe(2);
    expect(join).toHaveBeenCalledWith("At completion", undefined, { queue: "steering" });
    expect(await join.mock.results[0]!.value).toBe(false);
    const users = conversation.session.messages.filter((message) => message.role === "user");
    expect(users).toMatchObject([
      { role: "user", content: [{ type: "text", text: "Start" }], attribution: "user" },
      { role: "user", content: [{ type: "text", text: "At completion" }], attribution: "user" },
    ]);
    expect(users[1]!).not.toHaveProperty("steering");
    expect(
      await fixture.runtime.dispatch(
        IpcMethod.MESSAGE,
        { session_id: "boundary", message: "After completion" },
        () => {},
      ),
    ).toHaveProperty("accepted", false);
  } finally {
    await fixture.close();
  }
});

test("reviewer retains history and refreshes notes across rounds", async () => {
  const original = "The derivative is x.";
  const corrected = "The derivative is 2x.";
  const feedback = "Corrected the derivative to 2x. Check the saved correction.";
  const firstReport = JSON.stringify({
    correct: false,
    explanation: "The derivative needs correction.",
    confidence: 1,
    findings: [],
  });
  const finalReport = JSON.stringify({
    correct: true,
    explanation: "The saved derivative is correct.",
    confidence: 1,
    findings: [],
  });
  const reviewerRequests: ModelRequest[] = [];
  let writers = 0;
  const fixture = await localRuntime((body) => {
    if (JSON.stringify(body.messages).includes("REVIEW_CONTINUITY")) {
      reviewerRequests.push(body);
      const call = reviewerRequests.length;
      if (call === 1 || call === 3)
        return sse(modelTool("read", { path: "note_knowledge.md" }, `read-round-${call}`));
      return sse(modelText(call === 2 ? firstReport : finalReport));
    }
    ++writers;
    if (writers === 1) return sse(modelTool("review_notes", {}, "review-first"));
    if (writers === 2)
      return sse(
        modelTool("write", { path: "note_knowledge.md", content: corrected }, "correct-note"),
      );
    if (writers === 3) return sse(modelTool("review_notes", { message: feedback }, "review-again"));
    return sse(modelText("Reviewed correction"));
  });
  try {
    const files: Record<string, string> = {};
    for (const name of [
      "lecture_source.md",
      "note_knowledge.md",
      "note_gossip.md",
      "note_notice.md",
    ]) {
      files[name] = path.join(fixture.root, name);
      await Bun.write(files[name]!, name === "note_knowledge.md" ? original : "Notes");
    }
    const events: Record<string, unknown>[] = [];
    await fixture.runtime.dispatch(
      IpcMethod.CREATE_SESSION,
      {
        id: "continuity",
        tools: [WorkspaceTool.WRITE],
        files,
        meta: {
          review: {
            system: "REVIEW_CONTINUITY",
            prompt: "Review the saved notes",
            maxRounds: 2,
          },
        },
      },
      () => {},
    );
    const result = await fixture.runtime.dispatch(
      IpcMethod.RUN,
      { session_id: "continuity", instruction: "Review and correct the notes" },
      (name, data) => {
        if (name === RuntimeEvent.REVIEWER) events.push(data);
      },
    );
    expect(result).toMatchObject({
      final_message: "Reviewed correction",
      review_status: ReviewStatus.PASSED,
    });
    expect(writers).toBe(4);
    expect(reviewerRequests).toHaveLength(4);
    const secondRound = reviewerRequests[2]!;
    const priorReports = secondRound.messages.filter(
      (message) =>
        message.role === "assistant" &&
        JSON.stringify(message.content).includes("The derivative needs correction."),
    );
    expect(priorReports).toHaveLength(1);
    expect(userTexts(secondRound).at(-1)).toContain(`Writer message:\n${feedback}`);
    const readResult = (request: ModelRequest) =>
      JSON.stringify(request.messages.filter((message) => message.role === "tool").at(-1)?.content);
    expect(readResult(reviewerRequests[1]!)).toContain(original);
    expect(readResult(reviewerRequests[3]!)).toContain(corrected);
    expect(readResult(reviewerRequests[3]!)).not.toContain(original);
    const ready = events.filter((event) => event.event === RuntimeEvent.RUN_READY);
    expect(ready.map((event) => event.reviewer_id)).toEqual([
      "continuity-review",
      "continuity-review",
    ]);
    const finished = events.filter((event) => event.event === RuntimeEvent.REVIEW_FINISHED);
    expect(finished.map((event) => event.stale)).toEqual([false, false]);
    await fixture.runtime.dispatch(IpcMethod.CLOSE_SESSION, { session_id: "continuity" }, () => {});
    await expect(
      fixture.runtime.dispatch(
        IpcMethod.RUN,
        { session_id: "continuity-review", instruction: "Review after close" },
        () => {},
      ),
    ).rejects.toThrow("Unknown session: continuity-review");
  } finally {
    await fixture.close();
  }
});

test("reviewer messages change only the reviewer turn", async () => {
  const reviewerStarted = Promise.withResolvers<void>();
  let reviewers = 0;
  let writers = 0;
  let child = "";
  const fixture = await localRuntime((body) => {
    if (JSON.stringify(body.messages).includes("REVIEW_TARGET")) {
      if (++reviewers === 1)
        return new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(
                new TextEncoder().encode(
                  'data: {"id":"proof","choices":[{"index":0,"delta":{"role":"assistant","content":"Reviewing"},"finish_reason":null}]}\n\n',
                ),
              );
              reviewerStarted.resolve();
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      expect(userTexts(body).at(-1)).toBe(nativeSteeringText("Stop reviewing and finish now"));
      return sse(
        modelText(
          JSON.stringify({
            correct: true,
            explanation: "Checked",
            confidence: 1,
            findings: [],
          }),
        ),
      );
    }
    return sse(
      ++writers === 1 ? modelTool("review_notes", {}, "review-target") : modelText("Reviewed"),
    );
  });
  try {
    const files: Record<string, string> = {};
    for (const name of [
      "lecture_source.md",
      "note_knowledge.md",
      "note_gossip.md",
      "note_notice.md",
    ]) {
      files[name] = path.join(fixture.root, name);
      await Bun.write(files[name]!, "Notes");
    }
    const events: Record<string, unknown>[] = [];
    await fixture.runtime.dispatch(
      IpcMethod.CREATE_SESSION,
      {
        id: "parent",
        files,
        meta: {
          review: {
            system: "REVIEW_TARGET",
            prompt: "Review",
            maxRounds: 1,
          },
        },
      },
      () => {},
    );
    const run = fixture.runtime.dispatch(
      IpcMethod.RUN,
      { session_id: "parent", instruction: "Review notes" },
      (name, data) => {
        if (name === RuntimeEvent.REVIEWER) {
          events.push(data);
          if (data.event === RuntimeEvent.RUN_READY) child = String(data.reviewer_id);
        }
      },
    );
    await reviewerStarted.promise;
    expect(child).not.toBe("");
    expect(
      await fixture.runtime.dispatch(
        IpcMethod.MESSAGE,
        { session_id: child, message: "Stop reviewing and finish now" },
        () => {},
      ),
    ).toEqual({ accepted: true });
    expect(await run).toHaveProperty("final_message", "Reviewed");
    expect(writers).toBe(2);
    expect(reviewers).toBe(2);
    expect(events.find((event) => event.event === RuntimeEvent.REVIEW_FINISHED)).toHaveProperty(
      "stale",
      false,
    );
    expect(
      await fixture.runtime.dispatch(
        IpcMethod.MESSAGE,
        { session_id: child, message: "After completion" },
        () => {},
      ),
    ).toHaveProperty("accepted", false);
  } finally {
    await fixture.close();
  }
});

test("explicit stop cancels parent and reviewer without delivery", async () => {
  const reviewerStarted = Promise.withResolvers<void>();
  let reviewerId = "";
  const fixture = await localRuntime((body) => {
    if (!JSON.stringify(body.messages).includes("REVIEW_STOP"))
      return sse(modelTool("review_notes", {}, "review-stop"));
    return new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(
            new TextEncoder().encode(
              'data: {"id":"proof","choices":[{"index":0,"delta":{"role":"assistant","content":"Reviewing"},"finish_reason":null}]}\n\n',
            ),
          );
        },
      }),
      { headers: { "Content-Type": "text/event-stream" } },
    );
  });
  try {
    const files: Record<string, string> = {};
    for (const name of [
      "lecture_source.md",
      "note_knowledge.md",
      "note_gossip.md",
      "note_notice.md",
    ]) {
      files[name] = path.join(fixture.root, name);
      await Bun.write(files[name]!, "Notes");
    }
    await fixture.runtime.dispatch(
      IpcMethod.CREATE_SESSION,
      {
        id: "stop-parent",
        files,
        meta: {
          review: {
            system: "REVIEW_STOP",
            prompt: "Review",
            maxRounds: 1,
          },
        },
      },
      () => {},
    );
    const run = fixture.runtime.dispatch(
      IpcMethod.RUN,
      { session_id: "stop-parent", instruction: "Review notes" },
      (name, data) => {
        if (name === RuntimeEvent.REVIEWER) {
          reviewerId = String(data.reviewer_id);
          if (data.event === RuntimeEvent.DELTA) reviewerStarted.resolve();
        }
      },
    );
    const outcome = run.then(
      () => undefined,
      (error: unknown) => error,
    );
    await reviewerStarted.promise;
    await fixture.runtime.dispatch(IpcMethod.CANCEL, { session_id: "stop-parent" }, () => {});
    expect(String(await outcome)).toContain("cancelled");
    expect(
      await fixture.runtime.dispatch(
        IpcMethod.MESSAGE,
        { session_id: reviewerId, message: "Late" },
        () => {},
      ),
    ).toHaveProperty("accepted", false);
    await fixture.runtime.dispatch(
      IpcMethod.CLOSE_SESSION,
      { session_id: "stop-parent" },
      () => {},
    );
    await expect(
      fixture.runtime.dispatch(
        IpcMethod.RUN,
        { session_id: reviewerId, instruction: "Late" },
        () => {},
      ),
    ).rejects.toThrow("Unknown session");
  } finally {
    await fixture.close();
  }
});

test("reviewer corrects malformed reports in the same round", async () => {
  let reviewerCalls = 0;
  const fixture = await localRuntime((body) => {
    if (JSON.stringify(body.messages).includes("REVIEW_INVALID")) {
      ++reviewerCalls;
      if (reviewerCalls === 1) return sse(modelText("not valid JSON"));
      expect(userTexts(body).at(-1)).toContain("Invalid review report:");
      expect(JSON.stringify(body.messages)).toContain("not valid JSON");
      if (reviewerCalls === 2) return sse(modelText('{"correct":"yes"}'));
      expect(userTexts(body).at(-1)).toContain("correct");
      return sse(
        modelText(
          JSON.stringify({
            correct: true,
            explanation: "Checked",
            confidence: 1,
            findings: [],
          }),
        ),
      );
    }
    return sse(
      body.messages.some((message) => message.role === "tool")
        ? modelText("Complete")
        : modelTool("review_notes", {}, "invalid-review"),
    );
  });
  try {
    const files: Record<string, string> = {};
    for (const name of [
      "lecture_source.md",
      "note_knowledge.md",
      "note_gossip.md",
      "note_notice.md",
    ]) {
      files[name] = path.join(fixture.root, name);
      await Bun.write(files[name]!, "Notes");
    }
    const events: Record<string, unknown>[] = [];
    await fixture.runtime.dispatch(
      IpcMethod.CREATE_SESSION,
      {
        id: "invalid",
        files,
        meta: {
          review: {
            system: "REVIEW_INVALID",
            prompt: "Review",
            maxRounds: 1,
          },
        },
      },
      () => {},
    );
    const result = await fixture.runtime.dispatch(
      IpcMethod.RUN,
      { session_id: "invalid", instruction: "Review" },
      (name, data) => {
        if (name === RuntimeEvent.REVIEWER) events.push(data);
      },
    );
    expect(result).toHaveProperty("final_message", "Complete");
    expect(result).toHaveProperty("review_status", ReviewStatus.PASSED);
    expect(events.filter((event) => event.event === RuntimeEvent.REVIEW_STARTED)).toHaveLength(1);
    expect(events.filter((event) => event.event === RuntimeEvent.REVIEW_FINISHED)).toHaveLength(1);
    expect(events.some((event) => event.event === RuntimeEvent.REVIEW_FAILED)).toBe(false);
    const child = events.find((event) => event.event === RuntimeEvent.REVIEW_STARTED)?.reviewer_id;
    await fixture.runtime.dispatch(IpcMethod.CLOSE_SESSION, { session_id: "invalid" }, () => {});
    await expect(
      fixture.runtime.dispatch(IpcMethod.RUN, { session_id: child, instruction: "Late" }, () => {}),
    ).rejects.toThrow("Unknown session");
    expect(reviewerCalls).toBe(3);
  } finally {
    await fixture.close();
  }
});

test("IPC controls require exact message and reopen parameters", () => {
  expect(
    validate(
      ipcRequestSchema,
      { id: "m", method: IpcMethod.MESSAGE, params: { session_id: "writer", message: "a\nb" } },
      "request",
    ),
  ).toHaveProperty("params.message", "a\nb");
  expect(
    validate(
      ipcRequestSchema,
      { id: "r", method: IpcMethod.REOPEN_SESSION, params: { id: "writer", files: {} } },
      "request",
    ),
  ).toHaveProperty("method", IpcMethod.REOPEN_SESSION);
  expect(() =>
    validate(
      ipcRequestSchema,
      {
        id: "m",
        method: IpcMethod.MESSAGE,
        params: { session_id: "writer", message: "a", instruction: "extra" },
      },
      "request",
    ),
  ).toThrow("Invalid request");
});

test("IPC rejects unknown roles and tools", () => {
  expect(
    ipcRequestSchema.allows({
      id: "valid",
      method: IpcMethod.CREATE_SESSION,
      params: { id: "writer", role: AgentRole.NOTE_WRITER, tools: [WorkspaceTool.READ] },
    }),
  ).toBe(true);
  expect(
    ipcRequestSchema.allows({
      id: "role",
      method: IpcMethod.GENERATE,
      params: { role: "unknown_role", messages: [{ role: "user", content: "Read" }] },
    }),
  ).toBe(false);
  expect(
    ipcRequestSchema.allows({
      id: "tool",
      method: IpcMethod.CREATE_SESSION,
      params: { id: "writer", tools: ["unknown_tool"] },
    }),
  ).toBe(false);
});
