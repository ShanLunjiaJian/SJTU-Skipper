import { registerOAuthProvider } from "@oh-my-pi/pi-ai/oauth";
import { openCodeTokenResponseSchema, validate } from "./schemas";

export function registerOpenCode(id: string) {
  registerOAuthProvider({
    id,
    name: "OpenCode Go",
    async login() {
      throw new Error("Use the project device login action");
    },
    getApiKey: (credential) => credential.access,
    async refreshToken(credential, signal) {
      const response = await fetch("https://opencode.ai/console/auth/device/token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal,
        body: JSON.stringify({
          grant_type: "refresh_token",
          refresh_token: credential.refresh,
          client_id: "opencode-cli",
        }),
      });
      if (!response.ok) throw new Error(`OpenCode refresh failed: HTTP ${response.status}`);
      const body = validate(
        openCodeTokenResponseSchema,
        await response.json(),
        "OpenCode token response",
      );
      return {
        ...credential,
        access: body.access_token,
        refresh: body.refresh_token ?? credential.refresh,
        expires: Date.now() + body.expires_in * 1000,
        orgId: body.org_id ?? credential.orgId,
      };
    },
  });
}
