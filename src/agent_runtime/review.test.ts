import { test, expect } from "bun:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { ReviewGate } from "./review";

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
          overall_correctness: incorrect ? "incorrect" : "correct",
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
    const failed = await run();
    const content = failed.content.find((block) => block.type === "text");
    if (!content || content.type !== "text") throw new Error("Missing review report");
    const failedReport = JSON.parse(content.text);
    expect(failedReport.passed).toBe(false);
    expect(failedReport.review.overall_correctness).toBe("incorrect");
    expect(failedReport.review.findings).toHaveLength(1);
    expect(await gate.satisfied()).toBe(false);
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
    expect(prompts[1]).toContain("Missing theorem condition");
    expect(await gate.satisfied()).toBe(true);
    await Bun.write(path.join(directory, "note_notice.md"), "Changed after passing");
    expect(await gate.satisfied()).toBe(false);
    await run();
    expect(await gate.satisfied()).toBe(true);
    expect(seen.sort()).toEqual(
      ["note_review:Original", "note_review:Corrected", "note_review:Corrected"].sort(),
    );
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
