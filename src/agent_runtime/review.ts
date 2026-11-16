import { createHash } from "node:crypto";
import path from "node:path";
import { type } from "@oh-my-pi/pi-ai";
import type { ToolDefinition } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/types";

const NOTES = ["note_knowledge.md", "note_gossip.md", "note_notice.md"];
const reviewSchema = type({
  overall_correctness: "'correct' | 'incorrect'",
  explanation: "string",
  confidence: "0 <= number <= 1",
  findings: type({
    title: "string",
    body: "string",
    priority: "0 | 1 | 2 | 3",
    confidence: "0 <= number <= 1",
    file_path: "'note_knowledge.md' | 'note_gossip.md' | 'note_notice.md'",
    line_start: "number.integer >= 1",
    line_end: "number.integer >= 1",
  }).array(),
});
export interface ReviewOptions {
  prompt: string;
  system: string;
  maxRounds: number;
}
export class ReviewGate {
  private accepted?: string;
  private rounds = 0;
  constructor(
    private cwd: string,
    private options: ReviewOptions,
    private review: (
      role: string,
      prompt: string,
      files: Record<string, string>,
      signal?: AbortSignal,
    ) => Promise<string>,
  ) {}
  async digest() {
    const hash = createHash("sha256");
    for (const name of NOTES) {
      hash.update(name);
      hash.update(await Bun.file(path.join(this.cwd, name)).bytes());
    }
    return hash.digest("hex");
  }
  async satisfied() {
    return this.accepted !== undefined && this.accepted === (await this.digest());
  }
  tool(): ToolDefinition {
    return {
      name: "review_notes",
      label: "Review notes",
      approval: "read",
      loadMode: "essential",
      description:
        "Run the independent reviewer on the saved notes. After any edit, run this tool again. The overall correctness verdict must be correct.",
      parameters: type({}),
      execute: async (_id, _params, signal) => {
        if (this.options.maxRounds && this.rounds >= this.options.maxRounds)
          throw new Error("Lecture review round limit reached");
        ++this.rounds;
        this.accepted = undefined;
        const digest = await this.digest();
        const files: Record<string, string> = {};
        // The reviewer gets immutable copies, including the source used by the writer.
        for (const name of ["lecture_source.md", ...NOTES]) {
          const frozen = path.join(this.cwd, ".reviews", `${this.rounds}`, name);
          await Bun.write(frozen, await Bun.file(path.join(this.cwd, name)).bytes());
          files[name] = frozen;
        }
        const text = await this.review("note_review", this.options.prompt, files, signal);
        const report = reviewSchema(JSON.parse(text));
        if (report instanceof type.errors)
          throw new Error(`Invalid review report: ${report.summary}`);
        const passed = report.overall_correctness === "correct";
        if (passed && digest === (await this.digest())) this.accepted = digest;
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify({ passed: await this.satisfied(), review: report }),
            },
          ],
          details: {},
        };
      },
    };
  }
  exhausted() {
    return this.options.maxRounds > 0 && this.rounds >= this.options.maxRounds;
  }
}
