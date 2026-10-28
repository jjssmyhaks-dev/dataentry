import Link from "next/link";
import { Badge, Empty, PageHeader, Problem } from "@/components/ui";
import { IconPulse } from "@/components/icons";
import { engineGet, EngineError, type ActivityEntry } from "@/lib/engine";
import { currentSession } from "@/lib/session";
import { dateTime, relative } from "@/lib/format";

export const dynamic = "force-dynamic";

function summarise(detail: Record<string, unknown> | null): string {
  if (!detail) return "";
  const parts: string[] = [];
  for (const [key, value] of Object.entries(detail)) {
    if (value === null || value === undefined || value === "") continue;
    const text = typeof value === "object" ? JSON.stringify(value) : String(value);
    parts.push(`${key}: ${text.length > 80 ? `${text.slice(0, 80)}…` : text}`);
  }
  return parts.slice(0, 3).join(" · ");
}

export default async function ActivityPage({
  searchParams,
}: {
  searchParams: Promise<{ target?: string }>;
}) {
  const session = await currentSession();
  if (!session) return null;
  const params = await searchParams;

  let entries: ActivityEntry[] = [];
  let failure: EngineError | null = null;
  try {
    const data = await engineGet<{ entries: ActivityEntry[] }>("/v1/activity", session, {
      limit: 200,
      target_id: params.target,
    });
    entries = data.entries;
  } catch (error) {
    if (error instanceof EngineError) failure = error;
    else throw error;
  }

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Audit"
        title="Activity"
        description="Every review decision, autonomy change, agent command and post is recorded against the tenant, the user and the source document. This is the trail an auditor reads."
        actions={
          params.target ? (
            <Link href="/activity" className="btn btn-sm">
              Clear filter
            </Link>
          ) : null
        }
      />

      {failure ? (
        <Problem
          title={failure.isOffline ? "The engine is not running" : "Could not load the activity log"}
          detail={failure.detail}
        />
      ) : (
        <div className="panel overflow-hidden">
          {entries.length === 0 ? (
            <Empty title="Nothing recorded yet" icon={<IconPulse size={22} />}>
              Approvals, corrections, rejections and autonomy changes appear here as they happen.
            </Empty>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th className="w-[130px]">When</th>
                  <th>Action</th>
                  <th>Who</th>
                  <th>Target</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => (
                  <tr key={entry.id}>
                    <td className="mono text-[11px] text-ink-400">
                      {relative(entry.created_at)}
                      <div className="text-[10px] text-ink-600">{dateTime(entry.created_at)}</div>
                    </td>
                    <td>
                      <span className="mono text-[11.5px] text-teal-300/90">{entry.action}</span>
                      {entry.severity && entry.severity !== "info" ? (
                        <div className="mt-1">
                          <Badge tone={entry.severity === "error" ? "bad" : "warn"}>{entry.severity}</Badge>
                        </div>
                      ) : null}
                    </td>
                    <td className="text-[11.5px] text-ink-300">
                      <div>{entry.actor_type}</div>
                      <div className="mono max-w-[150px] truncate text-[10px] text-ink-500">
                        {entry.actor_id ?? "—"}
                      </div>
                    </td>
                    <td className="mono text-[10.5px] text-ink-400">
                      {entry.target_type ? (
                        <Link
                          href={`/activity?target=${entry.target_id}`}
                          className="hover:text-ink-200"
                        >
                          {entry.target_type}
                          <div className="max-w-[120px] truncate text-ink-600">
                            {entry.target_id ?? ""}
                          </div>
                        </Link>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="max-w-[360px] text-[11.5px] leading-snug text-ink-400">
                      {summarise(entry.detail) || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
