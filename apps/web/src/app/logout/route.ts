import { NextResponse } from "next/server";
import { SESSION_COOKIE } from "@/lib/session";

/**
 * Signing out clears the app session. The WorkOS session lives in this cookie
 * only, so no third-party logout round trip is needed.
 */
export async function GET(request: Request) {
  const url = new URL(request.url);
  const response = NextResponse.redirect(new URL("/signed-out", url.origin));
  response.cookies.set({ name: SESSION_COOKIE, value: "", path: "/", maxAge: 0 });
  return response;
}
