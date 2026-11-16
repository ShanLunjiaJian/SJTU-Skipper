import { Effort, type, type Type } from "@oh-my-pi/pi-ai";

const nonemptyString = type("string").atLeastLength(1);
const finiteNumber = type("number").narrow(Number.isFinite);
const sessionId = nonemptyString.matching(/^[A-Za-z0-9_-]+$/);
const stringMap = type({ "[string]": "string" });
const imageDataUrl = nonemptyString
  .matching(
    /^data:image\/[A-Za-z0-9.+-]+;base64,(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/,
  )
  .narrow((value) => value.slice(value.indexOf(",") + 1).length > 0);

export const textPartSchema = type({
  "+": "reject",
  type: "'text'",
  text: "string",
});
export const imagePartSchema = type({
  "+": "reject",
  type: "'image_url'",
  image_url: {
    "+": "reject",
    url: imageDataUrl,
    "detail?": "'auto' | 'low' | 'high'",
  },
});
const messageMetadata = {
  "name?": "string | null",
  "tool_calls?": type("never").array(),
  "tool_call_id?": "null",
} as const;
const systemMessageSchema = type({
  "+": "reject",
  ...messageMetadata,
  role: "'system'",
  content: type("string").or(textPartSchema.array()),
});
const conversationMessageSchema = type({
  "+": "reject",
  ...messageMetadata,
  role: "'user' | 'assistant' | 'developer'",
  content: type("string").or(textPartSchema.or(imagePartSchema).array()),
});

export const generateInputSchema = type({
  "+": "reject",
  messages: systemMessageSchema.or(conversationMessageSchema).array().atLeastLength(1),
  "role?": nonemptyString,
  "session_id?": sessionId.or("null"),
  "system_prompt?": "string",
  "max_tokens?": type("number.integer > 0").or("null"),
  "temperature?": finiteNumber.atLeast(0).or("null"),
  "reasoning?": type
    .enumerated(Effort.Minimal, Effort.Low, Effort.Medium, Effort.High, Effort.XHigh, Effort.Max)
    .or("'off' | 'auto'"),
});
export type GenerateInput = typeof generateInputSchema.infer;

const apiKeyCredentialSchema = type({
  "+": "reject",
  type: "'api_key'",
  key: nonemptyString,
  "source?": "'login'",
});
const oauthCredentialSchema = type({
  "+": "reject",
  type: "'oauth'",
  access: nonemptyString,
  refresh: nonemptyString,
  expires: finiteNumber.atLeast(0),
  "enterpriseUrl?": "string",
  "projectId?": "string",
  "email?": "string",
  "accountId?": "string",
  "apiEndpoint?": "string",
  "orgId?": "string",
  "org_id?": "string",
  "orgName?": "string",
  "region?": "string",
  "inferenceRegion?": "'global' | 'eu' | 'us'",
  "activeOrganizationId?": "string",
  "authorizedAt?": finiteNumber.atLeast(0),
}).pipe(({ org_id, ...credential }) => ({
  ...credential,
  ...((credential.orgId ?? org_id) === undefined ? {} : { orgId: credential.orgId ?? org_id }),
}));
export const credentialSchema = apiKeyCredentialSchema.or(oauthCredentialSchema);
export const credentialsSchema = type({ "[string]": credentialSchema });
export type Credential = typeof credentialSchema.infer;

// The SDK validates native model fields. Preserve them for its configuration parser.
const modelSchema = type({ id: nonemptyString, "[string]": "unknown" });
const providerSchema = type({
  "api?": "string",
  "auth?": "'apiKey' | 'oauth' | 'none'",
  "apiKey?": "string",
  "baseUrl?": "string",
  models: modelSchema.array().atLeastLength(1),
  "[string]": "unknown",
});
export const modelsConfigurationSchema = type({
  providers: type({ "[string]": providerSchema }).narrow(
    (providers) => Object.keys(providers).length > 0,
  ),
  "[string]": "unknown",
});
export const rolesConfigurationSchema = type({
  default: nonemptyString,
  "[string]": nonemptyString,
});

export const openCodeTokenResponseSchema = type({
  access_token: nonemptyString,
  "refresh_token?": nonemptyString,
  expires_in: finiteNumber.moreThan(0),
  "org_id?": "string",
  "token_type?": "string",
});

const emptyParams = type({ "+": "reject" }).default(() => ({}));
const configurationParams = type({
  "+": "reject",
  models: nonemptyString,
  roles: nonemptyString,
});
export const createSessionInputSchema = type({
  "+": "reject",
  id: sessionId,
  "role?": nonemptyString,
  "prompt?": "string",
  "files?": stringMap,
  "readonly?": "string[]",
  "tools?": type("'read' | 'grep' | 'glob' | 'edit' | 'write'").array(),
  "meta?": { "[string]": "unknown" },
});
const sessionParams = type({ "+": "reject", session_id: sessionId });
const providerParams = type({ "+": "reject", provider: nonemptyString });

export const ipcRequestHeaderSchema = type({ id: nonemptyString });
export const ipcRequestSchema = type({
  "+": "reject",
  id: nonemptyString,
  method: "'ping' | 'info'",
  params: emptyParams,
})
  .or({
    "+": "reject",
    id: nonemptyString,
    method: "'configure'",
    params: configurationParams,
  })
  .or({
    "+": "reject",
    id: nonemptyString,
    method: "'generate'",
    params: generateInputSchema,
  })
  .or({
    "+": "reject",
    id: nonemptyString,
    method: "'create_session'",
    params: createSessionInputSchema,
  })
  .or({
    "+": "reject",
    id: nonemptyString,
    method: "'run'",
    params: type({ "+": "reject", session_id: sessionId, instruction: "string" }),
  })
  .or({
    "+": "reject",
    id: nonemptyString,
    method: "'cancel' | 'close_session' | 'reset'",
    params: sessionParams,
  })
  .or({
    "+": "reject",
    id: nonemptyString,
    method: "'logout'",
    params: providerParams,
  })
  .or({
    "+": "reject",
    id: nonemptyString,
    method: "'credentials'",
    params: providerParams.merge({ key: nonemptyString }),
  })
  .or({
    "+": "reject",
    id: nonemptyString,
    method: "'import_credentials'",
    params: providerParams.merge({ credential: credentialSchema }),
  });
export type IpcRequest = typeof ipcRequestSchema.infer;

export function validate<T>(schema: Type<T>, value: unknown, label: string): T {
  const result = schema(value);
  // Schema diagnostics can include credentials or message contents.
  if (result instanceof type.errors) throw new Error(`Invalid ${label}`);
  return result;
}
