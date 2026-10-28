import { rejectException, retryException } from "@/app/actions/intake";
import { Badge, Empty, PageHeader, Problem } from "@/components/ui";
import { IconAlert, IconUpload } from "@/components/icons";
import { engineGet, EngineError, type ExceptionRow } from "@/lib/engine";
import { atLeast, currentSession } from "@/lib/session";
import { dateTime, relative } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function ExceptionsPage({
  searchParams,
}: {
  searchParams: Promise<{ ok?: string; error?: string }>;
}) {
  const session = await currentSession();
  if (!session) return null;
  const params = await searchParams;

  let rows: ExceptionRow[] = [];
  let failure: EngineError | null = null;
  try {
    const data = await engineGet<{ count: number; documents: ExceptionRow[] }>(
      "/v1/exceptions",
      session,
      { limit: 100 },
    );
    rows = data.documents;
  } catch (error) {
    if (error instanceof EngineError) failure = error;
    else throw error;
  }

  const canAct = atLeast(session.role, "reviewer");

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Review"
        title="Exceptions"
        description="Documents the engine could not process at all: unreadable files, unsupported formats, or a split that failed. Retry re-reads the stored text; reject closes the document with a reason that stays in the audit trail."
      />

      {params.ok ? (
        <div className="rounded-xl border border-emerald-500/25 bg-emerald-500/[0.07] px-3.5 py-2 text-[12.5px] text-emerald-200">
          {params.ok}
        </div>
      ) : null}
      {params.error ? (
        <div className="flex items-start gap-2 rounded-xl border border-rose-500/25 bg-rose-500/[0.07] px-3.5 py-2 text-[12.5px] text-rose-200">
          <IconAlert size={14} className="mt-0.5" /> {params.error}
        </div>
      ) : null}

      {failure ? (
        <Problem
          title={failure.isOffline ? "The engine is not running" : "Could not load exceptions"}
          detail={failure.detail}
        />
      ) : (
        <div className="panel overflow-hidden">
          {rows.length === 0 ? (
            <Empty title="No open exceptions" icon={<IconUpload size={22} />}>
              Every document that arrived was readable enough to classify. Anything unreadable will
              appear here with a plain-language reason.
            </Empty>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Document</th>
                  <th>Reason</th>
                  <th>Arrived</th>
                  <th className="text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td>
                      <div className="max-w-[240px] truncate text-[12.5px] text-ink-100">
                        {row.file_name ?? "unnamed file"}
                      </div>
                      <div className="mono mt-0.5 flex items-center gap-2 text-[10.5px] text-ink-500">
                        <Badge tone="muted">{row.intake_channel.replace(/_/g, " ")}</Badge>
                        <span>{row.sender ?? "sender unknown"}</span>
                      </div>
                    </td>
                    <td className="max-w-[380px] text-[12px] leading-snug text-ink-300">
                      {row.exception_reason ?? "No reason recorded"}
                    </td>
                    <td className="mono text-[11.5px] text-ink-400">
                      {relative(row.received_at)}
                      <div className="text-[10px] text-ink-600">{dateTime(row.received_at)}</div>
                    </td>
                    <td>
                      <div className="flex items-center justify-end gap-2">
                        <form action={retryException}>
                          <input type="hidden" name="document_id" value={row.id} />
                          <button className="btn btn-sm" disabled={!canAct}>
                            Retry
                          </button>
                        </form>
                        <form action={rejectException} className="flex items-center gap-1">
                          <input type="hidden" name="document_id" value={row.id} />
                          <input
                            className="input w-[150px] !py-1 !text-[11.5px]"
                            name="reason"
                            placeholder="reason"
                            defaultValue="not_a_business_document"
                          />
                          <button className="btn btn-sm btn-danger" disabled={!canAct}>
                            Reject
                          </button>
                        </form>
                      </div>
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
