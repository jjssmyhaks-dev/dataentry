import { NextResponse } from "next/server";
import { devSessionEnabled, workosConfigured } from "@/lib/env";
import { authorizationUrl } from "@/lib/workos";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const returnTo = url.searchParams.get("return_to") ?? "/queue";

  if (!workosConfigured) {
    // No keys yet: the demo organisation is used and the shell says so.
    if (devSessionEnabled) return NextResponse.redirect(new URL(returnTo, url.origin));
    return NextResponse.json(
      { detail: "WorkOS is not configured and development sessions are disabled." },
      { status: 503 },
    );
  }

  return NextResponse.redirect(authorizationUrl(returnTo));
}
