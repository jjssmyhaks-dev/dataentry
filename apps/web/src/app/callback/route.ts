import { NextResponse } from "next/server";
import { SESSION_COOKIE, sealSession } from "@/lib/session";
import { exchangeCode } from "@/lib/workos";
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

export async function GET(request: Request) {
  const url = new URL(request.url);
  const code = url.searchParams.get("code");
  const error = url.searchParams.get("error");

  if (error) {
    return NextResponse.redirect(
      new URL(`/signed-out?reason=${encodeURIComponent(error)}`, url.origin),
    );
  }
  if (!code) {
    return NextResponse.redirect(new URL("/signed-out?reason=missing_code", url.origin));
  }

  let session;
  let returnTo: string;
  try {
    const exchanged = await exchangeCode(code, readReturnTo(url.searchParams.get("state")));
    session = exchanged.session;
    returnTo = exchanged.returnTo;
  } catch (cause) {
    const message = cause instanceof Error ? cause.message : "sign-in failed";
    return NextResponse.redirect(
      new URL(`/signed-out?reason=${encodeURIComponent(message)}`, url.origin),
    );
  }

  const response = NextResponse.redirect(new URL(returnTo, url.origin));
  response.cookies.set({
    name: SESSION_COOKIE,
    value: sealSession(session),
    httpOnly: true,
    sameSite: "lax",
    secure: env.workosRedirectUri.startsWith("https://"),
    path: "/",
    maxAge: 60 * 60 * 8,
  });
  return response;
}
