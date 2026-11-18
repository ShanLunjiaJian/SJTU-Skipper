import { mkdir, readFile, writeFile, rename } from "node:fs/promises";
import path from "node:path";
import { AuthStorage } from "@oh-my-pi/pi-ai";
import { createAgentSession, Settings, SessionManager } from "@oh-my-pi/pi-coding-agent";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { resolveModelRoleValue } from "@oh-my-pi/pi-coding-agent/config/model-resolver";
import type { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import { USER_INTERRUPT_LABEL } from "@oh-my-pi/pi-coding-agent/session/messages";
import { parseSessionContent } from "@oh-my-pi/pi-coding-agent/session/session-loader";
import type { Model, Api } from "@oh-my-pi/pi-ai";
import { Workspace } from "./workspace";
import { resolveEditMode } from "@oh-my-pi/pi-coding-agent/utils/edit-mode";
import { registerOpenCode } from "./opencode";
import { generate } from "./generate";
import { compact as compactContext } from "@oh-my-pi/pi-agent-core/compaction";
import type { ExtensionFactory } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/types";
import { ReviewGate, ReviewStatus, parseReviewReport, type ReviewOptions } from "./review";
import { observeExecution } from "./execution";
import { providerView } from "./provider-view";
import {
  validate,
  AgentRole,
  IpcMethod,
  RuntimeEvent,
  ProviderSource,
  WorkspaceTool,
  workspaceToolSchema,
  agentRoleSchema,
  type Emit,
  modelsConfigurationSchema,
  rolesConfigurationSchema,
  credentialSchema,
} from "./schemas";

enum MessageRejectionReason {
  EMPTY = "Message is empty",
  INACTIVE = "Session has no active run",
}

const ROLE_NAMES = Object.values(AgentRole);
interface Configuration {
  models: string;
  roles: string;
  registry: ModelRegistry;
  allowed: Model<Api>[];
  selectors: Record<string, string>;
}
interface Conversation {
  run?: RunControl;
  closing?: Promise<void>;
  session: AgentSession;
  steps: number;
  role: AgentRole;
  workspace: Workspace;
  emit: Emit;
  review?: ReviewGate;
  reviewerId?: string;
  taskPrompt?: string;
}
interface RunControl {
  acceptingMessages: boolean;
  inbox: string[];
  generation: number;
  barrier?: Promise<void>;
  abortBarrier?: Promise<void>;
  termination?: Promise<void>;
  abortError?: unknown;
  cancelled: boolean;
  notification: PromiseWithResolvers<void>;
  done: PromiseWithResolvers<void>;
}

export class Runtime {
  private auth?: AuthStorage;
  private initialization?: Promise<void>;
  private config?: Configuration;
  private sessions = new Map<string, Conversation>();
  private generations = new Map<string, AbortController>();
  private opening = new Set<string>();
  private usage: Record<
    string,
    { input: number; output: number; cacheRead: number; totalTokens: number }
  > = {};
  constructor(private root: string) {}
  private async message(params: Record<string, unknown>) {
    const conversation = this.sessions.get(String(params.session_id));
    const control = conversation?.run;
    const text = params.message;
    if (typeof text !== "string" || !text.trim())
      return { accepted: false, reason: MessageRejectionReason.EMPTY };
    if (!conversation || !control?.acceptingMessages || control.cancelled || conversation.closing)
      return { accepted: false, reason: MessageRejectionReason.INACTIVE };
    ++control.generation;
    control.notification.resolve();
    return this.queueRunOperation(control, async () => {
      if (control.cancelled || !control.acceptingMessages)
        return { accepted: false, reason: MessageRejectionReason.INACTIVE };
      const admitted = await conversation.session.joinActiveTurn(text, undefined, {
        queue: "steering",
      });
      if (control.cancelled) return { accepted: false, reason: MessageRejectionReason.INACTIVE };
      if (admitted) void this.abortTurn(conversation, control);
      else control.inbox.push(text);
      control.notification.resolve();
      return { accepted: true };
    });
  }

  private queueRunOperation<T>(control: RunControl, operation: () => Promise<T>): Promise<T> {
    const result = (control.barrier ?? Promise.resolve()).then(operation);
    const barrier = result.then(
      () => {},
      (error) => {
        control.abortError ??= error;
      },
    );
    control.barrier = barrier;
    void barrier.then(() => {
      if (control.barrier === barrier) control.barrier = undefined;
      control.notification.resolve();
    });
    return result;
  }

  private abortTurn(conversation: Conversation, control: RunControl): Promise<void> {
    if (!control.abortBarrier) {
      const barrier = this.queueRunOperation(control, async () => {
        if (!control.cancelled)
          await conversation.session.abort({
            reason: USER_INTERRUPT_LABEL,
            deliverQueuedMessages: true,
          });
      });
      control.abortBarrier = barrier;
      void barrier.then(
        () => {
          if (control.abortBarrier === barrier) control.abortBarrier = undefined;
        },
        () => {
          if (control.abortBarrier === barrier) control.abortBarrier = undefined;
        },
      );
    }
    return control.abortBarrier;
  }

  private async settleRun(conversation: Conversation, control: RunControl): Promise<void> {
    for (;;) {
      await control.barrier;
      if (control.abortError) throw control.abortError;
      await conversation.session.waitForIdle();
      if (!control.barrier) return;
    }
  }

  private stopRun(conversation: Conversation, control: RunControl): Promise<void> {
    control.acceptingMessages = false;
    control.cancelled = true;
    control.inbox.length = 0;
    control.notification.resolve();
    // Withdraw input already claimed by the loop before abort can restore it.
    conversation.session.clearQueue({ forInterrupt: true });
    conversation.session.agent.clearAllQueues();
    control.termination ??= Promise.all([
      this.queueRunOperation(control, async () => {
        conversation.session.clearQueue({ forInterrupt: true });
        conversation.session.agent.clearAllQueues();
        await conversation.session.abort();
        await conversation.session.waitForIdle();
      }),
      conversation.review?.cancel(),
    ]).then(() => {});
    return control.termination;
  }

  private async terminate(conversation: Conversation): Promise<void> {
    const control = conversation.run;
    if (control) {
      await this.stopRun(conversation, control);
      await control.done.promise;
    } else {
      conversation.session.clearQueue({ forInterrupt: true });
      conversation.session.agent.clearAllQueues();
      await Promise.all([conversation.session.abort(), conversation.review?.cancel()]);
      await conversation.session.waitForIdle();
    }
  }

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
              `${role}: deepseek/deepseek-flash:${role === AgentRole.BOARD_RECOGNITION ? "off" : "xhigh"}\n`,
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

  private resolve(config: Configuration, role: AgentRole) {
    const result = resolveModelRoleValue(config.selectors[role] ?? "@default", config.allowed, {
      roleLookup: { getModelRole: (name: string) => config.selectors[name] },
    });
    if (!result.model || result.warning)
      throw new Error(result.warning ?? `Cannot resolve role: ${role}`);
    if (role === AgentRole.BOARD_RECOGNITION && !result.model.input.includes("image"))
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

  async dispatch(method: IpcMethod, params: Record<string, unknown>, emit: Emit): Promise<unknown> {
    if (method === IpcMethod.MESSAGE) return this.message(params);
    if (method === IpcMethod.CANCEL || method === IpcMethod.CLOSE_SESSION) {
      const conversation = this.sessions.get(String(params.session_id));
      if (conversation?.run) void this.stopRun(conversation, conversation.run).catch(() => {});
    }
    await this.initialize();
    if (method === IpcMethod.PING) return { bun: Bun.version };
    if (method === IpcMethod.GENERATE) {
      const id = String(params.session_id ?? crypto.randomUUID());
      const controller = new AbortController();
      this.generations.set(id, controller);
      try {
        const config = await this.load();
        controller.signal.throwIfAborted();
        const role = validate(
          agentRoleSchema,
          params.role ?? AgentRole.BOARD_RECOGNITION,
          "agent role",
        );
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
    if (method === IpcMethod.CONFIGURE) {
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
      return this.dispatch(IpcMethod.INFO, {}, emit);
    }
    if (method === IpcMethod.CREDENTIALS || method === IpcMethod.IMPORT_CREDENTIALS) {
      const credential =
        method === IpcMethod.CREDENTIALS
          ? { type: "api_key" as const, key: String(params.key) }
          : validate(credentialSchema, params.credential, "credential");
      await this.auth!.credentials.set(String(params.provider), credential);
      return { ok: true };
    }
    if (method === IpcMethod.LOGOUT) {
      await this.auth!.credentials.remove(String(params.provider));
      return { ok: true };
    }
    if (method === IpcMethod.INFO) {
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
        source: p.apiKey ? ProviderSource.CONFIG : ProviderSource.PROJECT,
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
    if (method === IpcMethod.CREATE_SESSION || method === IpcMethod.REOPEN_SESSION) {
      const config = await this.load();
      const id = String(params.id);
      const existing = this.sessions.get(id);
      if (existing) {
        if (method === IpcMethod.REOPEN_SESSION) throw new Error(`Session is already open: ${id}`);
        return { id, model: existing.session.model?.id ?? "" };
      }
      if (!/^[a-zA-Z0-9_-]+$/.test(id)) {
        throw new Error("Invalid session ID");
      }
      if (this.opening.has(id)) throw new Error(`Session is already opening: ${id}`);
      this.opening.add(id);
      try {
        const sessionDir = path.join(this.root, "sessions");
        const pointer = path.join(this.root, `session-${id}.path`);
        const saved =
          method === IpcMethod.REOPEN_SESSION && (await Bun.file(pointer).exists())
            ? (await Bun.file(pointer).text()).trim()
            : undefined;
        if (method === IpcMethod.REOPEN_SESSION && !saved?.trim())
          throw new Error(`Saved session transcript is unavailable: ${id}`);
        if (method === IpcMethod.REOPEN_SESSION && saved) {
          let loaded;
          try {
            loaded = parseSessionContent(await readFile(saved, "utf8"));
          } catch {
            throw new Error(`Saved session transcript is unreadable: ${id}`);
          }
          if (
            loaded.invalidHeader ||
            loaded.malformedRecords ||
            !loaded.entries.length ||
            !loaded.entries.some((entry) => entry.type === "message")
          )
            throw new Error(`Saved session transcript is invalid: ${id}`);
        }
        const role = validate(agentRoleSchema, params.role ?? AgentRole.DEFAULT, "agent role");
        const resolved = this.resolve(config, role);
        const compact = this.resolve(config, AgentRole.COMPACTION);
        const cwd = path.join(this.root, "workspaces", id);
        await mkdir(cwd, { recursive: true });
        const workspace = new Workspace(cwd);
        await workspace.initialize(
          (params.files ?? {}) as Record<string, string>,
          (params.readonly ?? []) as string[],
        );
        const settings = Settings.isolated({
          modelRoles: config.selectors,
          steeringMode: "all",
          followUpMode: "all",
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
          ? new ReviewGate(cwd, reviewOptions, async (role, prompt, files, signal, operation) => {
              const child = `${id}-review`;
              const childEvents: Emit = (name, data) =>
                this.sessions.get(id)?.emit(RuntimeEvent.REVIEWER, {
                  ...data,
                  session_id: id,
                  reviewer_id: child,
                  role,
                  event: name,
                  round: operation.round,
                  stale: operation.isStale(),
                });
              let cancellation: Promise<unknown> | undefined;
              let output: { final_message: string; final_message_block_ids: string[] } | undefined;
              const abort = () => {
                cancellation ??= this.dispatch(IpcMethod.CANCEL, { session_id: child }, emit);
                void cancellation.catch(() => {});
              };
              signal?.addEventListener("abort", abort, { once: true });
              try {
                signal?.throwIfAborted();
                childEvents(RuntimeEvent.REVIEW_STARTED, {});
                const reviewer = this.sessions.get(child);
                if (reviewer) {
                  await reviewer.workspace.initialize(files, Object.keys(files));
                } else {
                  await this.dispatch(
                    saved &&
                      (await Bun.file(path.join(this.root, `session-${child}.path`)).exists())
                      ? IpcMethod.REOPEN_SESSION
                      : IpcMethod.CREATE_SESSION,
                    {
                      id: child,
                      role,
                      prompt: reviewOptions.system,
                      files,
                      readonly: Object.keys(files),
                      tools: [WorkspaceTool.READ, WorkspaceTool.GREP, WorkspaceTool.GLOB],
                    },
                    childEvents,
                  );
                }
                if (signal?.aborted) {
                  await this.dispatch(IpcMethod.CANCEL, { session_id: child }, emit);
                  signal.throwIfAborted();
                }
                signal?.throwIfAborted();
                let instruction = prompt;
                for (;;) {
                  signal?.throwIfAborted();
                  const result = (await this.dispatch(
                    IpcMethod.RUN,
                    { session_id: child, instruction },
                    childEvents,
                  )) as {
                    error?: string;
                    final_message: string;
                    final_message_block_ids: string[];
                  };
                  output = result;
                  if (result.error) throw new Error(result.error);
                  signal?.throwIfAborted();
                  try {
                    parseReviewReport(result.final_message);
                  } catch (error) {
                    instruction = `${error instanceof Error ? error.message : String(error)}\nReturn a corrected JSON report that satisfies the review schema.`;
                    continue;
                  }
                  childEvents(RuntimeEvent.REVIEW_FINISHED, {
                    content: result.final_message,
                    block_ids: result.final_message_block_ids,
                  });
                  return result.final_message;
                }
              } catch (error) {
                childEvents(RuntimeEvent.REVIEW_FAILED, {
                  error: error instanceof Error ? error.message : String(error),
                  content: output?.final_message,
                  block_ids: output?.final_message_block_ids,
                });
                throw error;
              } finally {
                signal?.removeEventListener("abort", abort);
                await cancellation;
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
        const manager = saved
          ? await SessionManager.open(saved, sessionDir, undefined, {
              initialCwd: cwd,
              throwIfMissing: true,
            })
          : SessionManager.create(cwd, sessionDir);
        if (
          method === IpcMethod.REOPEN_SESSION &&
          !manager
            .getBranch()
            .some((entry) => entry.type === "message" && entry.message.role === "user")
        )
          throw new Error(`Saved session transcript has no conversation: ${id}`);
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
          toolNames: [
            ...((params.tools ?? []) as string[]),
            ...(gate ? [WorkspaceTool.REVIEW_NOTES] : []),
          ],
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
          assertDispatchModel: (model) => {
            if (!config.allowed.some((m) => m.provider === model.provider && m.id === model.id))
              throw new Error("Model is outside the project configuration");
          },
        });
        const sessionFile = manager.getSessionFile();
        if (sessionFile) await writeFile(pointer, sessionFile, { mode: 0o600 });
        for (const tool of result.session.agent.state.tools) {
          if (tool.name === WorkspaceTool.REVIEW_NOTES) continue;
          const execute = tool.execute.bind(tool);
          Object.defineProperty(tool, "openArgStream", { value: undefined, configurable: true });
          tool.execute = async (callId, args, signal, onUpdate, context) => {
            const mode = resolveEditMode({
              settings,
              getActiveModelString: () => resolved.model!.id,
            });
            await workspace.check(
              validate(workspaceToolSchema, tool.name, "workspace tool"),
              args,
              mode === "none" ? "replace" : mode,
            );
            let failed = false;
            try {
              return await execute(callId, args, signal, onUpdate, context);
            } catch (error) {
              failed = true;
              throw error;
            } finally {
              if (tool.name === WorkspaceTool.WRITE || tool.name === WorkspaceTool.EDIT) {
                try {
                  for (const change of await workspace.changes())
                    events(RuntimeEvent.FILE, { session_id: id, ...change });
                } catch (error) {
                  if (!failed) throw error;
                }
              }
            }
          };
        }
        this.sessions.set(id, {
          session: result.session,
          steps: 0,
          role,
          workspace,
          emit,
          review: gate,
          reviewerId: gate ? `${id}-review` : undefined,
          taskPrompt: saved ? undefined : String(params.prompt ?? ""),
        });
        return { id, model: result.session.model?.id ?? "" };
      } finally {
        this.opening.delete(id);
      }
    }
    const id = String(params.session_id);
    if (method === IpcMethod.CANCEL && this.generations.has(id)) {
      this.generations.get(id)!.abort();
      return { ok: true };
    }
    const conversation = this.sessions.get(id);
    if ((method === IpcMethod.CANCEL || method === IpcMethod.CLOSE_SESSION) && !conversation)
      return { ok: true };
    if (!conversation) throw new Error(`Unknown session: ${id}`);
    if (method === IpcMethod.CANCEL) {
      await this.terminate(conversation);
      return { ok: true };
    }
    if (method === IpcMethod.CLOSE_SESSION) {
      conversation.closing ??= (async () => {
        await this.terminate(conversation);
        if (conversation.reviewerId)
          await this.dispatch(
            IpcMethod.CLOSE_SESSION,
            { session_id: conversation.reviewerId },
            emit,
          );
        await conversation.session.dispose();
        this.sessions.delete(id);
      })();
      await conversation.closing;
      return { ok: true };
    }
    if (method === IpcMethod.RUN) {
      if (conversation.run || conversation.closing)
        throw new Error(`Session already has an active run: ${id}`);
      const control: RunControl = {
        acceptingMessages: true,
        inbox: [],
        generation: 0,
        cancelled: false,
        notification: Promise.withResolvers<void>(),
        done: Promise.withResolvers<void>(),
      };
      conversation.run = control;
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
            emit(RuntimeEvent.DELTA, { ...base, text: change.delta, delta: change.delta });
          if (change.type === "thinking_delta")
            emit(RuntimeEvent.REASONING, { ...base, text: change.delta, delta: change.delta });
        }
        if (event.type === "message_end" && event.message.role === "assistant") {
          this.recordUsage(event.message.provider, event.message.model, event.message.usage);
        }
        if (event.type === "tool_execution_start")
          emit(RuntimeEvent.TOOL_CALL, {
            ...base,
            name: event.toolName,
            arguments: event.args,
            tool_call_id: event.toolCallId,
          });
        if (event.type === "tool_execution_end") {
          emit(RuntimeEvent.TOOL_RESULT, {
            ...base,
            name: event.toolName,
            result: event.result,
            tool_call_id: event.toolCallId,
          });
        }
      });
      try {
        emit(RuntimeEvent.RUN_READY, { session_id: id });
        const task = [conversation.taskPrompt, String(params.instruction)]
          .filter(Boolean)
          .join("\n\n");
        let prompts = [task, ...((params.follow_ups ?? []) as string[])];
        conversation.taskPrompt = undefined;
        for (;;) {
          await this.settleRun(conversation, control);
          if (control.cancelled) throw new Error("Agent run cancelled");
          await conversation.review?.waitForPreparation();
          if (control.cancelled) throw new Error("Agent run cancelled");
          if (control.inbox.length) {
            prompts.push(...control.inbox.splice(0));
          }
          const delivery = !prompts.length ? conversation.review?.takeDelivery() : undefined;
          if (prompts.length || delivery) {
            const generation = control.generation;
            try {
              if (delivery) {
                await conversation.session.prompt(JSON.stringify(delivery), {
                  synthetic: true,
                  userInitiated: false,
                  expandPromptTemplates: false,
                  runCommands: false,
                });
              } else {
                const [prompt, ...followUps] = prompts;
                await conversation.session.prompt(prompt!, {
                  expandPromptTemplates: false,
                  runCommands: false,
                  userInitiated: true,
                  followUps,
                });
              }
            } catch (error) {
              const last = conversation.session.messages.findLast(
                (message) => message.role === "assistant",
              );
              const aborted =
                (error instanceof Error && error.name === "AbortError") ||
                (last?.role === "assistant" && last.stopReason === "aborted");
              if (control.cancelled || generation === control.generation || !aborted) throw error;
            }
            await this.settleRun(conversation, control);
            for (const change of await conversation.workspace.changes())
              emit(RuntimeEvent.FILE, { session_id: id, ...change });
            const last = conversation.session.messages.findLast(
              (message) => message.role === "assistant",
            );
            if (last?.role === "assistant" && last.stopReason === "error")
              throw new Error(last.errorMessage ?? "Model run failed");
            if (control.cancelled) throw new Error("Agent run cancelled");
            if (
              last?.role === "assistant" &&
              last.stopReason === "aborted" &&
              generation === control.generation
            )
              throw new Error(last.errorMessage ?? "Model run aborted");
            prompts = [];
            if (control.inbox.length) continue;
          }
          if (conversation.review?.hasPending()) {
            const notification = control.notification;
            const waiter = new AbortController();
            try {
              await Promise.race([
                conversation.review.waitForPending(waiter.signal),
                notification.promise,
              ]);
            } finally {
              waiter.abort();
              if (control.notification === notification)
                control.notification = Promise.withResolvers<void>();
            }
            continue;
          }
          const generation = control.generation;
          for (const change of await conversation.workspace.changes())
            emit(RuntimeEvent.FILE, { session_id: id, ...change });
          if (control.cancelled) throw new Error("Agent run cancelled");
          if (
            generation !== control.generation ||
            control.inbox.length ||
            control.barrier ||
            conversation.review?.hasPending()
          )
            continue;
          if (conversation.review) {
            const satisfied = await conversation.review.satisfied();
            if (control.cancelled) throw new Error("Agent run cancelled");
            // ``satisfied`` reads the note files, so a message may have been
            // admitted while it awaited: stay in the loop and deliver it
            // instead of completing the run with an undelivered inbox.
            if (
              generation !== control.generation ||
              control.inbox.length ||
              control.barrier ||
              conversation.review.hasPending()
            )
              continue;
            if (!satisfied) {
              if (conversation.review.exhausted())
                throw new Error(
                  "Review round limit reached without approval of the saved notes",
                );
              prompts = [
                "The saved notes do not have a passing review. Call review_notes, address its findings, and obtain approval for the saved notes before completing.",
              ];
              continue;
            }
          }
          control.acceptingMessages = false;
          break;
        }
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
        emit(RuntimeEvent.MESSAGE, { session_id: id, role: "assistant", content: text });
        const total_tokens = messages.reduce(
          (total, message) =>
            total + (message.role === "assistant" ? message.usage.totalTokens : 0),
          0,
        );
        return {
          session_id: id,
          final_message: text,
          final_message_block_ids: stopExecution.finalTextBlockIds(),
          steps,
          error,
          total_tokens,
          review_status: conversation.review
            ? await conversation.review.status()
            : ReviewStatus.PASSED,
        };
      } catch (error) {
        await this.stopRun(conversation, control);
        throw error;
      } finally {
        control.acceptingMessages = false;
        conversation.run = undefined;
        control.done.resolve();
        stopExecution();
        unsubscribe();
      }
    }
    throw new Error(`Unknown runtime method: ${method}`);
  }

  async close() {
    for (const controller of this.generations.values()) controller.abort();
    await Promise.all(
      [...this.sessions.keys()].map((id) =>
        this.dispatch(IpcMethod.CLOSE_SESSION, { session_id: id }, () => {}),
      ),
    );
    this.auth?.close();
  }
}
