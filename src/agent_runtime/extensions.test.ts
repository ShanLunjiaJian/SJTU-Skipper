import { expect, test } from "bun:test";
import { completionHooks } from "./extensions";

const report = JSON.stringify({
  correct: true,
  explanation: "Checked",
  confidence: 1,
  findings: [],
});

test("report rejection and plugin rejection preserve deterministic order", async () => {
  const calls: string[] = [];
  const reasons = await completionHooks(
    "not json",
    ["note.md"],
    [
      { owner: "b", id: "a", priority: 0 },
      { owner: "a", id: "z", priority: 0 },
      { owner: "a", id: "a", priority: 0 },
      { owner: "z", id: "first", priority: -1 },
    ],
    async (name) => {
      calls.push(name);
      return name === "a/a" ? { continue: "Read the saved note." } : null;
    },
  );
  expect(calls).toEqual(["z/first", "a/a", "a/z", "b/a"]);
  expect(reasons[0]).toContain("Invalid review report");
  expect(reasons[1]).toBe("[a/a] Read the saved note.");
});

test("hook failures cannot approve completion", async () => {
  await expect(
    completionHooks(report, ["note.md"], [{ owner: "a", id: "check", priority: 0 }], async () => {
      throw new Error("Check failed");
    }),
  ).rejects.toThrow("Check failed");
  await expect(
    completionHooks(report, ["note.md"], [{ owner: "a", id: "check", priority: 0 }], async () => ({
      continue: " ",
    })),
  ).rejects.toThrow("Invalid completion hook result");
});

test("ordinary roles do not require a JSON review report", async () => {
  const reasons = await completionHooks(
    "Plain answer",
    undefined,
    [{ owner: "a", id: "output", priority: 0 }],
    async () => null,
  );
  expect(reasons).toEqual([]);
});
