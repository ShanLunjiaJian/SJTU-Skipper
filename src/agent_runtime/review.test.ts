import { test, expect } from "bun:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { ReviewGate, ReviewStatus, parseReviewReport } from "./review";

test("edits require the unified reviewer again", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "note-review-"));
  try {
    for (const name of [
      "lecture_source.md",
      "note_knowledge.md",
      "note_gossip.md",
      "note_notice.md",
    ])
      await Bun.write(path.join(directory, name), "Original");
    const seen: string[] = [];
    const prompts: string[] = [];
    let fail = true;
    const gate = new ReviewGate(
      directory,
      {
        prompt: "coverage and correctness",
        system: "Review",
        maxRounds: 3,
      },
      async (role, prompt, files) => {
        seen.push(`${role}:${await Bun.file(files["note_knowledge.md"]!).text()}`);
        prompts.push(prompt);
        const incorrect = fail;
        return JSON.stringify({
          correct: !incorrect,
          explanation: incorrect ? "Missing theorem condition" : "No blocking defects",
          confidence: 1,
          findings: [
            {
              title: incorrect ? "Restore condition" : "Shorten title",
              body: incorrect
                ? "Source requires the condition; omission changes the theorem."
                : "Optional wording with unchanged meaning.",
              priority: incorrect ? 2 : 3,
              confidence: 1,
              file_path: "note_knowledge.md",
              line_start: 1,
              line_end: 1,
            },
          ],
        });
      },
    );
    const tool = gate.tool();
    const run = () => tool.execute("review", {}, undefined, undefined, {} as never);
    expect(await gate.satisfied()).toBe(false);
    expect(await gate.status()).toBe(ReviewStatus.NOT_REVIEWED);
    const failed = await run();
    const content = failed.content.find((block) => block.type === "text");
    if (!content || content.type !== "text") throw new Error("Missing review report");
    const failedReport = JSON.parse(content.text);
    expect(failedReport.passed).toBe(false);
    expect(failedReport.report.correct).toBe(false);
    expect(failedReport.report.findings).toHaveLength(1);
    expect(failedReport).not.toHaveProperty("review");
    expect(await gate.satisfied()).toBe(false);
    expect(await gate.status()).toBe(ReviewStatus.FAILED);
    await Bun.write(path.join(directory, "note_knowledge.md"), "Corrected");
    // A writer's final reply does not change the gate.
    expect(await gate.satisfied()).toBe(false);
    fail = false;
    await tool.execute(
      "review",
      { message: "Restored the source condition. Recheck the theorem." },
      undefined,
      undefined,
      {} as never,
    );
    expect(prompts[1]).toContain("Restored the source condition. Recheck the theorem.");
    expect(await gate.satisfied()).toBe(true);
    expect(await gate.status()).toBe(ReviewStatus.PASSED);
    await Bun.write(path.join(directory, "note_notice.md"), "Changed after passing");
    expect(await gate.satisfied()).toBe(false);
    expect(await gate.status()).toBe(ReviewStatus.NOT_REVIEWED);
    await run();
    expect(await gate.satisfied()).toBe(true);
    expect(seen.sort()).toEqual(
      ["note_review:Original", "note_review:Corrected", "note_review:Corrected"].sort(),
    );
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

const correct = JSON.stringify({
  correct: true,
  explanation: "Checked",
  confidence: 1,
  findings: [],
});

async function reviewFiles(directory: string) {
  for (const name of ["lecture_source.md", "note_knowledge.md", "note_gossip.md", "note_notice.md"])
    await Bun.write(path.join(directory, name), "Original");
}

test("a message-interrupted wait preserves acceptance and delivers once", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "review-live-"));
  const started = Promise.withResolvers<void>();
  const finish = Promise.withResolvers<string>();
  let reviewerSignal: AbortSignal | undefined;
  let calls = 0;
  try {
    await reviewFiles(directory);
    const gate = new ReviewGate(
      directory,
      { prompt: "Review", system: "Review", maxRounds: 2 },
      async (_role, _prompt, _files, signal) => {
        ++calls;
        reviewerSignal = signal;
        started.resolve();
        return calls === 1 ? finish.promise : correct;
      },
    );
    const abort = new AbortController();
    const pending = gate.tool().execute("r1", {}, abort.signal, undefined, {} as never);
    await started.promise;
    abort.abort();
    const result = await pending;
    expect(result.content).toEqual([{ type: "text", text: '{"pending":true}' }]);
    expect(reviewerSignal?.aborted).toBe(false);
    expect(gate.hasPending()).toBe(true);
    finish.resolve(correct);
    await gate.waitForPending();
    const delivery = gate.takeDelivery();
    expect(delivery).toMatchObject({ round: 1, stale: false });
    expect(gate.takeDelivery()).toBeUndefined();
    expect(await gate.satisfied()).toBe(true);
    expect(calls).toBe(1);
    expect(gate.hasPending()).toBe(false);
  } finally {
    finish.resolve(correct);
    await rm(directory, { recursive: true, force: true });
  }
});

test("snapshot changes mark reviewer events stale", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "review-stale-"));
  try {
    await reviewFiles(directory);
    let stale = false;
    const gate = new ReviewGate(
      directory,
      { prompt: "Review", system: "Review", maxRounds: 1 },
      async (_role, _prompt, files, _signal, operation) => {
        expect(operation.round).toBe(1);
        expect(operation.isStale()).toBe(false);
        await Bun.write(path.join(directory, "note_notice.md"), "Changed");
        expect(await Bun.file(files["note_notice.md"]!).text()).toBe("Original");
        stale = operation.isStale();
        return correct;
      },
    );
    await gate.tool().execute("r", {}, undefined, undefined, {} as never);
    expect(stale).toBe(true);
    expect(await gate.satisfied()).toBe(false);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("review failures remain deliverable and cancellation waits", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "review-cancel-"));
  try {
    await reviewFiles(directory);
    const started = Promise.withResolvers<void>();
    const stopped = Promise.withResolvers<void>();
    const gate = new ReviewGate(
      directory,
      { prompt: "Review", system: "Review", maxRounds: 2 },
      async (_role, _prompt, _files, signal) => {
        started.resolve();
        signal!.addEventListener("abort", () => stopped.resolve(), { once: true });
        await stopped.promise;
        throw new Error("Reviewer stopped");
      },
    );
    const pending = gate.tool().execute("r", {}, undefined, undefined, {} as never);
    await started.promise;
    await gate.cancel();
    await pending;
    expect(gate.hasPending()).toBe(false);
    expect(await gate.satisfied()).toBe(false);
    expect(() => parseReviewReport("not json")).toThrow("Invalid review report");
    expect(() => parseReviewReport('{"correct":true}')).toThrow("Invalid review report");
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
