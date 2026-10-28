import { createCipheriv, createDecipheriv, createHash, randomBytes } from "node:crypto";
import { cookies } from "next/headers";
import { env, devSessionEnabled } from "./env";

/**
 * The WorkOS session, sealed into an encrypted cookie.
 *
 * The organisation id in here is the only thing that decides which tenant's
 * schema a request touches. It is resolved on the server and sent to the engine;
 * the browser can never name a tenant (section 12c).
 */

export const SESSION_COOKIE = "autopost_session";

export type Role = "owner" | "reviewer" | "viewer";

export interface Session {
  userId: string;
  email: string;
  name: string;
  organizationId: string | null;
  organizationName: string | null;
  role: Role;
  /** True only for the local demo session, so the UI can say so plainly. */
  demo: boolean;
}

const ROLE_RANK: Record<Role, number> = { viewer: 1, reviewer: 2, owner: 3 };

export function isRole(value: unknown): value is Role {
  return value === "owner" || value === "reviewer" || value === "viewer";
}

export function normaliseRole(input: unknown): Role {
  const slug = String(input ?? "").toLowerCase();
  if (slug.includes("owner") || slug.includes("admin")) return "owner";
  if (slug.includes("review") || slug.includes("approv") || slug.includes("member")) return "reviewer";
  return "viewer";
}

export function atLeast(role: Role, needed: Role): boolean {
  return ROLE_RANK[role] >= ROLE_RANK[needed];
}

function key(): Buffer {
  return createHash("sha256").update(env.cookiePassword).digest();
}

export function sealSession(session: Session): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", key(), iv);
  const payload = Buffer.from(JSON.stringify(session), "utf8");
  const body = Buffer.concat([cipher.update(payload), cipher.final()]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([iv, tag, body]).toString("base64url");
}

export function openSession(sealed: string): Session | null {
  try {
    const raw = Buffer.from(sealed, "base64url");
    const iv = raw.subarray(0, 12);
    const tag = raw.subarray(12, 28);
    const body = raw.subarray(28);
    const decipher = createDecipheriv("aes-256-gcm", key(), iv);
    decipher.setAuthTag(tag);
    const json = Buffer.concat([decipher.update(body), decipher.final()]).toString("utf8");
    const parsed = JSON.parse(json) as Session;
    if (!parsed.userId) return null;
    return { ...parsed, role: normaliseRole(parsed.role) };
  } catch {
    return null;
  }
}

export async function readSession(): Promise<Session | null> {
  const store = await cookies();
  const raw = store.get(SESSION_COOKIE)?.value;
  if (!raw) return null;
  return openSession(raw);
}

/** The demo session used before the WorkOS keys are supplied. */
export function demoSession(): Session {
  return {
    userId: "dev-owner",
    email: "owner@design-partner.local",
    name: "Design partner owner",
    organizationId: null,
    organizationName: "Precision Pipes (demo organisation)",
    role: "owner",
    demo: true,
  };
}

export async function currentSession(): Promise<Session | null> {
  const session = await readSession();
  if (session) return session;
  if (devSessionEnabled) return demoSession();
  return null;
}

export async function requireSession(): Promise<Session> {
  const session = await currentSession();
  if (!session) throw new Error("not signed in");
  return session;
}
