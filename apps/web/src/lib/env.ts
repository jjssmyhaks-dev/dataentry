/**
 * Server-side configuration. Nothing here is exposed to the browser: the engine
 * service token never leaves the server, and the browser only ever sees the
 * signed session cookie.
 */

function optional(name: string): string | undefined {
  const value = process.env[name];
  return value && value.trim() ? value.trim() : undefined;
}

export const env = {
  /** Base URL of the FastAPI engine that owns every tenant schema. */
  engineUrl: (optional("ENGINE_API_URL") ?? "http://127.0.0.1:8088").replace(/\/$/, ""),
  engineToken: optional("ENGINE_SERVICE_TOKEN") ?? "",
  /** Tenant used when WorkOS is not configured yet (local development only). */
  devTenant: optional("ENGINE_DEV_TENANT") ?? "precision-pipes",

  workosApiKey: optional("WORKOS_API_KEY"),
  workosClientId: optional("WORKOS_CLIENT_ID"),
  workosRedirectUri:
    optional("WORKOS_REDIRECT_URI") ?? "http://localhost:3000/callback",

  /** Encrypts the session cookie. A generated local key is fine in development. */
  cookiePassword:
    optional("WORKOS_COOKIE_PASSWORD") ??
    "local-development-only-cookie-password-change-me",

  /**
   * When the WorkOS keys are absent the app runs as a single demo organisation so
   * the queue can be worked on. The engine refuses this path in production.
   */
  allowDevSession: (optional("ALLOW_DEV_SESSION") ?? "true") === "true",

  appName: "Autopost",
} as const;

export const workosConfigured = Boolean(env.workosApiKey && env.workosClientId);

/** Dev sessions are only possible when WorkOS is unconfigured *and* allowed. */
export const devSessionEnabled = !workosConfigured && env.allowDevSession;
