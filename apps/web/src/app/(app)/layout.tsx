import { redirect } from "next/navigation";
import { Sidebar, TopBar } from "@/components/shell";
import { engineGet, type AutonomyState, type Dashboard } from "@/lib/engine";
import { currentSession } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function AppLayout({ children }: { children: React.ReactNode }) {
  const session = await currentSession();
  if (!session) redirect("/login");

  let mode: string | null = null;
  let stage: number | null = null;
  let counts = null;
  let exceptions: number | null = null;
  let engineUp = false;

  try {
    const autonomy = await engineGet<AutonomyState>("/v1/autonomy", session);
    mode = autonomy.posting_mode;
    stage = autonomy.stage.stage;
    engineUp = true;
  } catch {
    engineUp = false;
  }

  if (engineUp) {
    try {
      const dashboard = await engineGet<Dashboard>("/v1/dashboard", session, { days: 30 });
      counts = dashboard.queue;
      exceptions = dashboard.documents.exceptions;
    } catch {
      /* the page itself reports the failure */
    }
  }

  return (
    <div className="flex min-h-screen">
      <Sidebar counts={counts} exceptions={exceptions} />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar session={session} mode={mode} stage={stage} engineUp={engineUp} />
        {session.demo ? (
          <div className="border-b border-amber-500/20 bg-amber-500/[0.06] px-5 py-2 text-[12px] text-amber-200/90">
            <strong className="font-semibold">Demo organisation.</strong> WorkOS is not configured yet,
            so this session is the seeded design-partner tenant and every user is treated as its owner.
            Add <span className="mono">WORKOS_API_KEY</span> and{" "}
            <span className="mono">WORKOS_CLIENT_ID</span> to sign in with real organisations and roles.
          </div>
        ) : null}
        <main className="mx-auto w-full max-w-[1500px] flex-1 px-5 py-6 lg:px-8">{children}</main>
      </div>
    </div>
  );
}
