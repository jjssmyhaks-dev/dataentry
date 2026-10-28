import Link from "next/link";
import { workosConfigured } from "@/lib/env";

export default async function SignedOutPage({
  searchParams,
}: {
  searchParams: Promise<{ reason?: string }>;
}) {
  const params = await searchParams;

  return (
    <div className="grid min-h-screen place-items-center px-6">
      <div className="panel w-full max-w-sm p-7 text-center">
        <div className="mx-auto grid h-10 w-10 place-items-center">
          <svg width="34" height="34" viewBox="0 0 26 26" fill="none" aria-hidden="true">
            <rect width="26" height="26" rx="8" className="fill-foreground" />
            <path
              d="M8 17.5 13 8l5 9.5"
              stroke="var(--color-background)"
              strokeWidth="2.2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </div>
        <h1 className="mt-4 text-[17px] font-semibold text-ink-100">Signed out</h1>
        <p className="mt-1.5 text-[12.5px] leading-relaxed text-ink-400">
          {params.reason
            ? `The sign-in did not complete: ${params.reason}`
            : "Your session has ended. Sign in with your organisation to continue."}
        </p>
        <Link href="/login" className="btn btn-primary mt-5 w-full">
          Sign in
        </Link>
        {!workosConfigured ? (
          <p className="mt-4 text-[11px] leading-relaxed text-ink-500">
            WorkOS keys are not configured yet, so signing in returns to the seeded demo organisation.
          </p>
        ) : null}
        <Link href="/" className="mt-5 block text-[11.5px] text-ink-400 hover:text-ink-200">
          Back to autopost.app
        </Link>
      </div>
    </div>
  );
}
