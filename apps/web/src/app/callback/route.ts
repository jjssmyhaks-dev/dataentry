import { NextResponse } from "next/server";
import { SESSION_COOKIE, sealSession, type Session } from "@/lib/session";
import { exchangeCode } from "@/lib/workos";
import { ensureTenant } from "@/lib/tenants";
import { env } from "@/lib/env";

function readReturnTo(state: string | null): string {
  if (!state) return "/queue";
  try {
    const parsed = JSON.parse(Buffer.from(state, "base64url").toString("utf8")) as {
      returnTo?: string;
    };
    const target = parsed.returnTo ?? "/queue";
    // Only same-origin relative paths, so the callback cannot be used as an open redirect.
    return target.startsWith("/") && !target.startsWith("//") ? target : "/queue";
  } catch {
    return "/queue";
  }
}

function fail(origin: string, reason: string) {
  return NextResponse.redirect(new URL(`/signed-out?reason=${encodeURIComponent(reason)}`, origin));
}

export async function GET(request: Request) {
  const url = new URL(request.url);
  const code = url.searchParams.get("code");
  const error = url.searchParams.get("error");

  if (error) return fail(url.origin, error);
  if (!code) return fail(url.origin, "missing_code");

  let session: Session;
  let returnTo: string;
  try {
    const exchanged = await exchangeCode(code, readReturnTo(url.searchParams.get("state")));
    session = exchanged.session;
    returnTo = exchanged.returnTo;
  } catch (cause) {
    const message = cause instanceof Error ? cause.message : "sign-in failed";
    return fail(url.origin, message);
  }

  // First sign-in from a new organisation creates its tenant and schema before
  // anything in the console can be opened (section 12c). This is idempotent: the
  // organisation id is the key, so the second person from the same customer
  // resolves to the tenant that already exists.
  let organizationName = session.organizationName;
  let provisioned: string | null = null;
  try {
    const result = await ensureTenant(session);
    organizationName = organizationName ?? result.tenant.name;
    provisioned = result.created ? result.tenant.slug : null;
  } catch (cause) {
    const message = cause instanceof Error ? cause.message : "tenant provisioning failed";
    return fail(url.origin, message);
  }

  const sealed = sealSession({
    ...session,
    organizationName,
    // The schema is chosen on the server from the session's organisation, so the
    // signed-in tenant is recorded here for the shell and the audit line.
    tenantSlug: provisioned ?? null,
  });

  const response = NextResponse.redirect(new URL(returnTo, url.origin));
  response.cookies.set({
    name: SESSION_COOKIE,
    value: sealed,
    httpOnly: true,
    sameSite: "lax",
    secure: env.workosRedirectUri.startsWith("https://"),
    path: "/",
    maxAge: 60 * 60 * 8,
  });
  return response;
}
