/**
 * Normalise an OpenAI-compatible `/models` response into the model fields this
 * application configures: id, context window, output limit, input modalities
 * and reasoning support.
 *
 * Endpoints advertise wildly different shapes.  OpenCode adds `limit` and
 * `modalities`; OpenRouter-style gateways add `context_length` and
 * `architecture.input_modalities`; a plain OpenAI-compatible server may only
 * send `{ id }`.  Missing capability fields keep the defaults below so the
 * result is always a usable models.yml entry that the UI can refine.
 */

export interface DiscoveredModel {
  id: string;
  name?: string;
  contextWindow: number;
  maxTokens: number;
  input: ("text" | "image")[];
  reasoning: boolean;
}

const DEFAULT_CONTEXT_WINDOW = 128_000;
const DEFAULT_MAX_TOKENS = 8_192;

type Row = Record<string, unknown>;

function obj(value: unknown): Row | undefined {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Row) : undefined;
}

function num(...values: unknown[]): number | undefined {
  for (const value of values) {
    const parsed = typeof value === "string" ? Number(value) : value;
    if (typeof parsed === "number" && Number.isFinite(parsed) && parsed > 0)
      return Math.floor(parsed);
  }
  return undefined;
}

function modal(row: Row): string[] {
  const modalities = obj(row.modalities);
  const architecture = obj(row.architecture);
  const raw = modalities?.input ?? architecture?.input_modalities ?? row.input;
  if (!Array.isArray(raw)) return [];
  return raw.map((item) => String(item).toLowerCase());
}

function supportsImage(values: string[]): boolean {
  return values.some((value) => value.startsWith("image"));
}

function supportsReasoning(row: Row): boolean {
  const capabilities = obj(row.capabilities);
  const reasoning = obj(capabilities?.reasoning);
  if (reasoning) return reasoning.supported !== false;
  return Boolean(row.reasoning);
}

function isChat(row: Row): boolean {
  const type = row.type;
  return type === undefined || type === null || String(type) === "chat";
}

export function normalizeModels(body: unknown): DiscoveredModel[] {
  const rows = obj(body)?.data;
  if (!Array.isArray(rows)) return [];
  const seen = new Set<string>();
  const models: DiscoveredModel[] = [];
  for (const item of rows) {
    const row = obj(item);
    const id = row ? String(row.id ?? "").trim() : "";
    if (!row || !id || seen.has(id) || !isChat(row)) continue;
    seen.add(id);
    const input = modal(row);
    const name = typeof row.name === "string" ? row.name.trim() : "";
    models.push({
      id,
      ...(name ? { name } : {}),
      contextWindow:
        num(
          obj(row.limit)?.context,
          row.context_window,
          row.context_length,
          row.max_context_length,
        ) ?? DEFAULT_CONTEXT_WINDOW,
      maxTokens:
        num(obj(row.limit)?.output, row.max_completion_tokens, row.max_output_tokens) ??
        DEFAULT_MAX_TOKENS,
      input: supportsImage(input) ? ["text", "image"] : ["text"],
      reasoning: supportsReasoning(row),
    });
  }
  return models.sort((a, b) => a.id.localeCompare(b.id));
}
