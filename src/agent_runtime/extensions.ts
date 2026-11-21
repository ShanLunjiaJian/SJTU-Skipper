import { type } from "@oh-my-pi/pi-ai";
import type { ToolDefinition } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/types";
import { parseReviewReport } from "./review";
import type { SessionExtensions } from "./schemas";

export interface CallbackRequest {
  session_id: string;
  role: string;
  kind: "tool" | "completion_hook";
  name: string;
  arguments: Record<string, unknown>;
}
export type RuntimeCallback = (request: CallbackRequest, signal?: AbortSignal) => Promise<unknown>;
export interface ToolCallRecord {
  name: string;
  arguments: unknown;
  result: unknown;
}
const hookResultSchema = type({ continue: type("string").atLeastLength(1) });
const toolResultSchema = type({
  content: type({ type: "'text'", text: "string" }).array(),
  details: { "[string]": "unknown" },
  "isError?": "boolean",
});
interface CompletionHook {
  name: string;
  run: () => Promise<{ continue: string } | null>;
}

export async function completionHooks(
  text: string,
  targets: readonly string[] | undefined,
  hooks: NonNullable<SessionExtensions["completion_hooks"]>,
  invoke: (name: string) => Promise<unknown>,
): Promise<string[]> {
  const pipeline: CompletionHook[] = [];
  if (targets !== undefined)
    pipeline.push({
      name: "review/report",
      run: async () => {
        try {
          parseReviewReport(text, targets);
          return null;
        } catch (error) {
          return {
            continue: `${error instanceof Error ? error.message : String(error)}\nReturn a corrected JSON report that satisfies the review schema.`,
          };
        }
      },
    });
  const ordered = [...hooks].sort(
    (a, b) =>
      a.priority - b.priority ||
      (a.owner < b.owner ? -1 : a.owner > b.owner ? 1 : 0) ||
      (a.id < b.id ? -1 : a.id > b.id ? 1 : 0),
  );
  const names = new Set<string>();
  for (const hook of ordered) {
    const name = `${hook.owner}/${hook.id}`;
    if (names.has(name)) throw new Error(`Duplicate completion hook: ${name}`);
    names.add(name);
    pipeline.push({
      name,
      run: async () => {
        const result = await invoke(name);
        if (result === null) return null;
        const parsed = hookResultSchema.assert(result);
        if (!parsed.continue.trim()) throw new Error(`Invalid completion hook result: ${name}`);
        return parsed;
      },
    });
  }
  const reasons: string[] = [];
  for (const hook of pipeline) {
    const result = await hook.run();
    if (result) reasons.push(`[${hook.name}] ${result.continue}`);
  }
  return reasons;
}

export function extensionTools(
  tools: NonNullable<SessionExtensions["tools"]>,
  invoke: (name: string, parameters: unknown, signal?: AbortSignal) => Promise<unknown>,
): ToolDefinition[] {
  return tools.map((tool) => ({
    name: tool.name,
    label: tool.label ?? tool.name,
    description: tool.description,
    parameters: tool.parameters as ToolDefinition["parameters"],
    loadMode: "essential",
    execute: async (_id, parameters, signal) => {
      const result = await invoke(tool.name, parameters, signal);
      return toolResultSchema.assert(result);
    },
  }));
}
