import { mkdir, readFile, writeFile, rename } from "node:fs/promises";
import path from "node:path";
import { AuthStorage } from "@oh-my-pi/pi-ai";
import { createAgentSession, Settings, SessionManager } from "@oh-my-pi/pi-coding-agent";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { resolveModelRoleValue } from "@oh-my-pi/pi-coding-agent/config/model-resolver";
import type { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import type { Model, Api } from "@oh-my-pi/pi-ai";
import { Workspace } from "./workspace";
import { resolveEditMode } from "@oh-my-pi/pi-coding-agent/utils/edit-mode";
import { registerOpenCode } from "./opencode";
import { generate } from "./generate";
import { compact as compactContext } from "@oh-my-pi/pi-agent-core/compaction";
import type { ExtensionFactory } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/types";
import { ReviewGate, type ReviewOptions } from "./review";
import { observeExecution } from "./execution";
import { providerView } from "./provider-view";
import {
  validate,
  modelsConfigurationSchema,
  rolesConfigurationSchema,
  credentialSchema,
} from "./schemas";

type Emit = (name: string, data: Record<string, unknown>) => void;
const ROLE_NAMES = [
  "default",
  "board_recognition",
  "note_chunk",
  "note_writer",
  "note_review",
  "note_revision",
  "ask_instant",
  "ask_research",
  "compaction",
];
interface Configuration {
  models: string;
  roles: string;
  registry: ModelRegistry;
  allowed: Model<Api>[];
  selectors: Record<string, string>;
}
interface Conversation {
  session: AgentSession;
  steps: number;
  role: string;
  workspace: Workspace;
  emit: Emit;
  review?: ReviewGate;
}

export class Runtime {
  private auth?: AuthStorage;
  private initialization?: Promise<void>;
  private config?: Configuration;
  private sessions = new Map<string, Conversation>();
  private generations = new Map<string, AbortController>();
  private usage: Record<
    string,
    { input: number; output: number; cacheRead: number; totalTokens: number }
  > = {};
  constructor(private root: string) {}

  private async initialize() {
    this.initialization ??= (async () => {
      await mkdir(this.root, { recursive: true });
      for (const [name, text] of [
        [
          "models.yml",
          "providers:\n  deepseek:\n    baseUrl: https://api.deepseek.com\n    api: openai-completions\n    auth: apiKey\n    models:\n      - id: deepseek-flash\n        input:\n          - text\n          - image\n        reasoning: true\n        contextWindow: 1000000\n        maxTokens: 384000\n",
        ],
        [
          "roles.yml",
          ROLE_NAMES.map(
            (role) =>
              `${role}: deepseek/deepseek-flash:${role === "board_recognition" ? "off" : "xhigh"}\n`,
          ).join(""),
        ],
      ]) {
        try {
          await writeFile(path.join(this.root, name), text, { flag: "wx", mode: 0o600 });
        } catch (error) {
          if (!(error instanceof Error && "code" in error && error.code === "EEXIST")) throw error;
        }
      }
      this.auth = await AuthStorage.create(path.join(this.root, "credentials.db"));
      await this.auth.credentials.reload();
    })();
    await this.initialization;
  }

  private recordUsage(
    provider: string,
    model: string,
    usage: { input: number; output: number; cacheRead: number; totalTokens: number },
  ) {
    const total = (this.usage[`${provider}/${model}`] ??= {
      input: 0,
      output: 0,
      cacheRead: 0,
      totalTokens: 0,
    });
    total.input += usage.input;
    total.output += usage.output;
    total.cacheRead += usage.cacheRead;
    total.totalTokens += usage.totalTokens;
  }

  private async requestModel(model: Model<Api>, sessionId: string): Promise<Model<Api>> {
    if (!model.baseUrl.startsWith("https://opencode.ai/inference/go/")) return model;
    const credential = await this.auth!.oauth.access(model.provider, sessionId);
    if (!credential?.orgId) throw new Error("OpenCode organization is missing; sign in again");
    return {
      ...model,
      headers: {
        ...model.headers,
        "x-opencode-org-id": credential.orgId,
        "x-opencode-session": sessionId,
        "x-session-affinity": sessionId,
        "X-Session-Id": sessionId,
        "x-opencode-session-id": sessionId,
      },
    };
  }

  private async configuration(models: string, roles: string): Promise<Configuration> {
    await this.initialize();
    const parsed = validate(modelsConfigurationSchema, Bun.YAML.parse(models), "models.yml");
    const selectors = validate(rolesConfigurationSchema, Bun.YAML.parse(roles), "roles.yml");
    for (const [provider, value] of Object.entries(parsed.providers)) {
      if (!value.models?.length) throw new Error(`Provider ${provider} requires models`);
      if (
        "baseUrl" in value &&
        String(value.baseUrl).startsWith("https://opencode.ai/inference/go/")
      )
        registerOpenCode(provider);
    }
    for (const role of ROLE_NAMES) {
      const seen = new Set<string>();
      let value = selectors[role] ?? selectors.default;
      while (value.startsWith("@")) {
        const target = value.slice(1);
        if (seen.has(target) || !(target in selectors))
          throw new Error(`Invalid role alias: ${role}`);
        seen.add(target);
        value = selectors[target];
      }
    }
    const candidate = path.join(this.root, `models-${crypto.randomUUID()}.yml`);
    const registryInput = structuredClone(parsed);
    for (const provider of Object.values(registryInput.providers!)) {
      // The SDK requires an inline key for apiKey mode. Project storage resolves it per request.
      if ((provider.auth ?? "apiKey") === "apiKey" && !provider.apiKey) provider.auth = "oauth";
    }
    await writeFile(candidate, Bun.YAML.stringify(registryInput), { mode: 0o600 });
    const settings = Settings.isolated({ modelRoles: selectors });
    const registry = new ModelRegistry(this.auth!, candidate, {
      settings,
      cacheDbPath: path.join(this.root, "models.db"),
    });
    if (registry.getError())
      throw new Error("Invalid models.yml: provider or model configuration failed validation");
    const allowed = Object.entries(parsed.providers).flatMap(([provider, value]) =>
      value.models!.map((m) => {
        const model = registry.find(provider, m.id);
        if (!model) throw new Error(`Invalid model: ${provider}/${m.id}`);
        return model;
      }),
    );
    const config = {
      models: Bun.YAML.stringify(parsed, null, 2),
      roles: Bun.YAML.stringify(selectors, null, 2),
      registry,
      allowed,
      selectors,
    };
    for (const role of ROLE_NAMES) this.resolve(config, role);
    return config;
  }

  private resolve(config: Configuration, role: string) {
    const result = resolveModelRoleValue(config.selectors[role] ?? "@default", config.allowed, {
      roleLookup: { getModelRole: (name: string) => config.selectors[name] },
    });
    if (!result.model || result.warning)
      throw new Error(result.warning ?? `Cannot resolve role: ${role}`);
    if (role === "board_recognition" && !result.model.input.includes("image"))
      throw new Error("board_recognition requires image input");
    return result;
  }

  private async load() {
    if (this.config) return this.config;
    const models = await readFile(path.join(this.root, "models.yml"), "utf8");
    const roles = await readFile(path.join(this.root, "roles.yml"), "utf8");
    this.config = await this.configuration(models, roles);
    return this.config;
  }

  async dispatch(method: string, params: Record<string, unknown>, emit: Emit): Promise<unknown> {
    await this.initialize();
    if (method === "ping") return { bun: Bun.version };
    if (method === "generate") {
      const id = String(params.session_id ?? crypto.randomUUID());
      const controller = new AbortController();
      this.generations.set(id, controller);
      try {
        const config = await this.load();
        controller.signal.throwIfAborted();
        const role = String(params.role ?? "board_recognition");
        const resolved = this.resolve(config, role);
        const response = await generate(
          await this.requestModel(resolved.model!, id),
          config.registry.resolver(resolved.model!, id),
          { ...params, ...(resolved.thinkingLevel ? { reasoning: resolved.thinkingLevel } : {}) },
          emit,
          controller.signal,
        );
        this.recordUsage(response.provider, response.model, response.usage);
        return response;
      } finally {
        this.generations.delete(id);
      }
    }
    if (method === "configure") {
      const config = await this.configuration(String(params.models), String(params.roles));
      const previous = this.config;
      const entries = [
        ["models.yml", config.models],
        ["roles.yml", config.roles],
      ];
      try {
        for (const [name, text] of entries) {
          await writeFile(path.join(this.root, `${name}.tmp`), text, { mode: 0o600 });
        }
        for (const [name] of entries) {
          await rename(path.join(this.root, `${name}.tmp`), path.join(this.root, name));
        }
      } catch (error) {
        if (previous) {
          await writeFile(path.join(this.root, "models.yml"), previous.models, { mode: 0o600 });
          await writeFile(path.join(this.root, "roles.yml"), previous.roles, { mode: 0o600 });
        }
        throw error;
      }
      this.config = config;
      return this.dispatch("info", {}, emit);
    }
    if (method === "credentials" || method === "import_credentials") {
      const credential =
        method === "credentials"
          ? { type: "api_key" as const, key: String(params.key) }
          : validate(credentialSchema, params.credential, "credential");
      await this.auth!.credentials.set(String(params.provider), credential);
      return { ok: true };
    }
    if (method === "logout") {
      await this.auth!.credentials.remove(String(params.provider));
      return { ok: true };
    }
    if (method === "info") {
      const config = await this.load();
      const definitions = validate(
        modelsConfigurationSchema,
        Bun.YAML.parse(config.models),
        "models.yml",
      );
      const providers = Object.entries(definitions.providers).map(([id, p]) => ({
        id,
        auth: p.auth ?? "apiKey",
        configured: p.auth === "none" || !!p.apiKey || this.auth!.credentials.list(id).length > 0,
        source: p.apiKey ? "config" : "project",
      }));
      const roles = Object.fromEntries(
        ROLE_NAMES.map((role) => {
          const r = this.resolve(config, role);
          return [
            role,
            { provider: r.model!.provider, model: r.model!.id, thinkingLevel: r.thinkingLevel },
          ];
        }),
      );
      return {
        models_yaml: config.models,
        roles_yaml: config.roles,
        providers,
        roles,
        usage: this.usage,
      };
    }
    if (method === "create_session") {
      const config = await this.load();
      const id = String(params.id);
      const existing = this.sessions.get(id);
      if (existing) return { id, model: existing.session.model?.id ?? "" };
      if (!/^[a-zA-Z0-9_-]+$/.test(id)) {
        throw new Error("Invalid session ID");
      }
      const role = String(params.role ?? "default");
      const resolved = this.resolve(config, role);
      const compact = this.resolve(config, "compaction");
      const cwd = path.join(this.root, "workspaces", id);
      await mkdir(cwd, { recursive: true });
      const workspace = new Workspace(cwd);
      await workspace.initialize(
        (params.files ?? {}) as Record<string, string>,
        (params.readonly ?? []) as string[],
      );
      const settings = Settings.isolated({
        modelRoles: config.selectors,
        "tools.format": "auto",
        "edit.mode": "hashline",
        "autolearn.enabled": false,
        skillful: false,
        includeWorkspaceTree: false,
        "compaction.enabled": true,
        systemPromptTemplates: Object.fromEntries(
          config.allowed.map((model) => [
            `${model.provider}/${model.id}`,
            path.join(import.meta.dir, "prompts", "classroom.md"),
          ]),
        ),
      });
      const reviewOptions = (params.meta as { review?: ReviewOptions } | undefined)?.review;
      const gate = reviewOptions
        ? new ReviewGate(cwd, reviewOptions, async (role, prompt, files, signal) => {
            const child = `${id}-review-${crypto.randomUUID()}`;
            const childEvents: Emit = (name, data) =>
              this.sessions.get(id)?.emit("agent.reviewer", {
                ...data,
                session_id: id,
                reviewer_id: child,
                role,
                event: name,
              });
            childEvents("started", {});
            await this.dispatch(
              "create_session",
              {
                id: child,
                role,
                prompt: reviewOptions.system,
                files,
                readonly: Object.keys(files),
                tools: ["read", "grep", "glob"],
              },
              childEvents,
            );
            const abort = () => {
              void this.dispatch("cancel", { session_id: child }, emit);
            };
            signal?.addEventListener("abort", abort, { once: true });
            try {
              signal?.throwIfAborted();
              const result = (await this.dispatch(
                "run",
                { session_id: child, instruction: prompt },
                childEvents,
              )) as { error?: string; final_message: string };
              if (result.error) throw new Error(result.error);
              childEvents("finished", { content: result.final_message });
              return result.final_message;
            } catch (error) {
              childEvents("failed", {
                error: error instanceof Error ? error.message : String(error),
              });
              throw error;
            } finally {
              signal?.removeEventListener("abort", abort);
              await this.dispatch("close_session", { session_id: child }, emit);
            }
          })
        : undefined;
      const events: Emit = (name, data) => this.sessions.get(id)?.emit(name, data);
      const compaction: ExtensionFactory = (api) => {
        api.on("before_provider_request", (event) =>
          providerView(event.payload, [cwd, this.root, process.cwd()]),
        );
        api.on("session_before_compact", async (event) => ({
          compaction: await compactContext(
            event.preparation,
            await this.requestModel(compact.model!, id),
            config.registry.resolver(compact.model!, id),
            event.customInstructions,
            event.signal,
            {
              thinkingLevel: compact.thinkingLevel === "auto" ? undefined : compact.thinkingLevel,
              sessionId: id,
            },
          ),
        }));
      };
      const sessionDir = path.join(this.root, "sessions");
      const pointer = path.join(this.root, `session-${id}.path`);
      const saved = (await Bun.file(pointer).exists()) ? await Bun.file(pointer).text() : undefined;
      const manager = saved
        ? await SessionManager.open(saved, sessionDir, undefined, {
            initialCwd: cwd,
            throwIfMissing: true,
          })
        : SessionManager.create(cwd, sessionDir);
      const result = await createAgentSession({
        cwd,
        agentId: id,
        agentName: role,
        agentDir: this.root,
        settings,
        authStorage: this.auth,
        modelRegistry: config.registry,
        model: {
          ...(await this.requestModel(resolved.model!, id)),
          compactionModel: `${compact.model!.provider}/${compact.model!.id}`,
        },
        thinkingLevel: resolved.thinkingLevel,
        sessionManager: manager,
        toolNames: [...((params.tools ?? []) as string[]), ...(gate ? ["review_notes"] : [])],
        restrictToolNames: true,
        allowRestrictedCustomTools: true,
        customTools: gate ? [Object.assign(gate.tool(), { __isToolDefinition: true })] : [],
        preloadedPreparedExtensions: [
          {
            path: "<classroom-compaction>",
            resolvedPath: "<classroom-compaction>",
            factory: compaction,
            error: null,
          },
        ],
        skills: [],
        rules: [],
        contextFiles: [],
        promptTemplates: [],
        slashCommands: [],
        noContextFiles: true,
        disableExtensionDiscovery: true,
        enableMCP: false,
        enableLsp: false,
        enableIrc: false,
        skipPythonPreflight: true,
        cacheWarming: false,
        disableModelFallback: true,
        disableServerSideFallback: true,
        appendSystemPrompt: String(params.prompt ?? ""),
        assertDispatchModel: (model) => {
          if (!config.allowed.some((m) => m.provider === model.provider && m.id === model.id))
            throw new Error("Model is outside the project configuration");
        },
      });
      const sessionFile = manager.getSessionFile();
      if (sessionFile) await writeFile(pointer, sessionFile, { mode: 0o600 });
      for (const tool of result.session.agent.state.tools) {
        if (tool.name === "review_notes") continue;
        const execute = tool.execute.bind(tool);
        Object.defineProperty(tool, "openArgStream", { value: undefined, configurable: true });
        tool.execute = async (callId, args, signal, onUpdate, context) => {
          const mode = resolveEditMode({
            settings,
            getActiveModelString: () => resolved.model!.id,
          });
          await workspace.check(tool.name, args, mode === "none" ? "replace" : mode);
          const output = await execute(callId, args, signal, onUpdate, context);
          if (tool.name === "write" || tool.name === "edit") {
            for (const change of await workspace.changes())
              events("agent.file", { session_id: id, ...change });
          }
          return output;
        };
      }
      this.sessions.set(id, {
        session: result.session,
        steps: 0,
        role,
        workspace,
        emit,
        review: gate,
      });
      return { id, model: result.session.model?.id ?? "" };
    }
    const id = String(params.session_id);
    if (method === "cancel" && this.generations.has(id)) {
      this.generations.get(id)!.abort();
      return { ok: true };
    }
    const conversation = this.sessions.get(id);
    if (method === "cancel" && !conversation) return { ok: true };
    if (!conversation) throw new Error(`Unknown session: ${id}`);
    if (method === "cancel") {
      await conversation.session.abort();
      return { ok: true };
    }
    if (method === "close_session") {
      await conversation.session.dispose();
      this.sessions.delete(id);
      return { ok: true };
    }
    if (method === "run") {
      conversation.emit = emit;
      let steps = 0;
      const stopExecution = observeExecution(conversation.session, emit, id);
      const unsubscribe = conversation.session.subscribe((event) => {
        const base = { session_id: id, step: steps };
        if (event.type === "turn_start") {
          ++steps;
        }
        if (event.type === "message_update") {
          const change = event.assistantMessageEvent;
          if (change.type === "text_delta")
            emit("agent.delta", { ...base, text: change.delta, delta: change.delta });
          if (change.type === "thinking_delta")
            emit("agent.reasoning", { ...base, text: change.delta, delta: change.delta });
        }
        if (event.type === "message_end" && event.message.role === "assistant") {
          this.recordUsage(event.message.provider, event.message.model, event.message.usage);
        }
        if (event.type === "tool_execution_start")
          emit("agent.tool_call", {
            ...base,
            name: event.toolName,
            arguments: event.args,
            tool_call_id: event.toolCallId,
          });
        if (event.type === "tool_execution_end") {
          emit("agent.tool_result", {
            ...base,
            name: event.toolName,
            result: event.result,
            tool_call_id: event.toolCallId,
          });
        }
      });
      try {
        await conversation.session.prompt(String(params.instruction));
        while (conversation.review && !(await conversation.review.satisfied())) {
          const last = conversation.session.messages.findLast(
            (message) => message.role === "assistant",
          );
          if (
            last?.role === "assistant" &&
            (last.stopReason === "error" || last.stopReason === "aborted")
          )
            break;
          if (conversation.review.exhausted())
            throw new Error("Lecture review did not pass before the round limit");
          await conversation.session.prompt(
            "Completion rejected: the reviewer must judge the saved notes correct. Call review_notes. Check the evidence for blocking findings before revising. Call review_notes again after every edit.",
          );
        }
        for (const change of await conversation.workspace.changes())
          emit("agent.file", { session_id: id, ...change });
        const messages = conversation.session.messages;
        const last = [...messages].reverse().find((m) => m.role === "assistant");
        const text =
          last?.role === "assistant"
            ? last.content
                .filter((p) => p.type === "text")
                .map((p) => p.text)
                .join("\n")
            : "";
        const error =
          last?.role === "assistant" && last.stopReason !== "stop"
            ? (last.errorMessage ?? `Model run did not finish: ${last.stopReason}`)
            : "";
        emit("agent.message", { session_id: id, role: "assistant", content: text });
        const total_tokens = messages.reduce(
          (total, message) =>
            total + (message.role === "assistant" ? message.usage.totalTokens : 0),
          0,
        );
        return { session_id: id, final_message: text, steps, error, total_tokens };
      } finally {
        stopExecution();
        unsubscribe();
      }
    }
    throw new Error(`Unknown runtime method: ${method}`);
  }

  async close() {
    for (const controller of this.generations.values()) controller.abort();
    await Promise.all(
      [...this.sessions.values()].map(async (c) => {
        await c.session.abort();
        await c.session.dispose();
      }),
    );
    this.auth?.close();
  }
}
