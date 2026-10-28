import Link from "next/link";
import { QueueFilters } from "@/components/queue-filters";
import { Badge, ConfidenceBar, Empty, PageHeader, Problem } from "@/components/ui";
import { IconArrowRight, IconInbox } from "@/components/icons";
import { engineGet, EngineError, type QueueResponse, type QueueRow } from "@/lib/engine";
import { currentSession } from "@/lib/session";
import { age, fieldLabel, money, number, relative } from "@/lib/format";

export const dynamic = "force-dynamic";

const PAGE_SIZE = 25;

const STATUS_TABS = [
  { key: "pending,held,clarification_requested", label: "Open" },
  { key: "pending", label: "Pending" },
  { key: "held", label: "Held" },
  { key: "clarification_requested", label: "Clarification" },
  { key: "approved,corrected", label: "Approved" },
  { key: "rejected", label: "Rejected" },
  { key: "auto_posted", label: "Auto-posted" },
];

function reasonHeadline(item: QueueRow): { code: string; message: string } {
  const reasons = item.reasons ?? [];
  // Draft and shadow mode are context, not the trigger: show the rule or field
  // that actually sent the record here.
  const contextual = new Set(["mode_draft", "mode_shadow", "would_have_posted", "all_checks_passed"]);
  const trigger = reasons.find((reason) => !contextual.has(reason.code)) ?? reasons[0];
  if (trigger) return { code: trigger.code, message: trigger.message };
  return { code: "unknown", message: "Flagged with no recorded reason" };
}

export default async function QueuePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | undefined>>;
}) {
  const session = await currentSession();
  if (!session) return null;
  const params = await searchParams;

  const status = params.status ?? STATUS_TABS[0].key;
  const offset = Math.max(0, Number(params.offset ?? 0) || 0);

  let data: QueueResponse | null = null;
  let failure: EngineError | null = null;
  try {
    data = await engineGet<QueueResponse>("/v1/review/queue", session, {
      status,
      doc_type: params.doc_type,
      band: params.band,
      reason: params.reason,
      vendor: params.vendor,
      min_amount: params.min_amount,
      limit: PAGE_SIZE,
      offset,
    });
  } catch (error) {
    if (error instanceof EngineError) failure = error;
    else throw error;
  }

  if (failure || !data) {
    return (
      <div className="space-y-6">
        <PageHeader eyebrow="Review" title="Queue" />
        <Problem
          title={failure?.isOffline ? "The engine is not running" : "Could not load the queue"}
          detail={failure?.detail ?? "No response from the engine."}
        />
      </div>
    );
  }

  const openCount =
    (data.counts.pending?.count ?? 0) +
    (data.counts.held?.count ?? 0) +
    (data.counts.clarification_requested?.count ?? 0);

  function href(overrides: Record<string, string | undefined>) {
    const next = new URLSearchParams();
    for (const [key, value] of Object.entries({ ...params, ...overrides })) {
      if (value) next.set(key, value);
    }
    return `/queue?${next.toString()}`;
  }

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Review"
        title="Review queue"
        description={
          <>
            {number(openCount)} open records, {number(data.counts.pending?.rule_failures ?? 0)} from a
            failed rule. Sorted by what matters most: rule failures first, then low confidence, then
            high value, then oldest. A reviewer should clear a record in under 30 seconds.
          </>
        }
        actions={
          <>
            <Link href="/bulk" className="btn">
              Bulk approve
            </Link>
            <Link href={href({ status: "pending,held,clarification_requested" })} className="btn btn-primary">
              Work the open queue
            </Link>
          </>
        }
      />

      <div className="flex flex-wrap items-center gap-1.5">
        {STATUS_TABS.map((tab) => {
          const active = status === tab.key;
          const count = tab.key.includes(",")
            ? openCount
            : (data.counts[tab.key]?.count ?? 0);
          return (
            <Link
              key={tab.key}
              href={href({ status: tab.key, offset: undefined })}
              className={`flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-[12.5px] transition-colors ${
                active
                  ? "border-foreground bg-foreground text-background"
                  : "border-border bg-card text-ink-400 hover:bg-muted hover:text-foreground"
              }`}
            >
              {tab.label}
              <span className={`mono text-[11px] ${active ? "text-background/70" : "text-ink-500"}`}>
                {count}
              </span>
            </Link>
          );
        })}
      </div>

      <QueueFilters
        docType={params.doc_type ?? ""}
        band={params.band ?? ""}
        reason={params.reason ?? ""}
        vendor={params.vendor ?? ""}
        minAmount={params.min_amount ?? ""}
      />

      <div className="panel overflow-hidden">
        {data.items.length === 0 ? (
          <Empty title="Nothing here" icon={<IconInbox size={24} />}>
            No records match these filters. Try the open tab, or clear the filters.
          </Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th className="w-[86px]">Band</th>
                <th>Record</th>
                <th>Why it is here</th>
                <th className="text-right">Amount</th>
                <th>Confidence</th>
                <th className="text-right">Waiting</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.items.map((item) => {
                const reason = reasonHeadline(item);
                return (
                  <tr key={item.id} className="group">
                    <td>
                      {item.priority_band === "rule_failure" ? (
                        <Badge tone="bad">rule</Badge>
                      ) : item.priority_band === "low_confidence" ? (
                        <Badge tone="warn">confidence</Badge>
                      ) : (
                        <Badge tone="muted">routine</Badge>
                      )}
                    </td>
                    <td>
                      <Link
                        href={`/queue/${item.id}`}
                        className="block max-w-[260px] truncate text-[13px] font-medium text-ink-100 group-hover:text-teal-200"
                      >
                        {item.vendor_name ?? "Unnamed vendor"}
                      </Link>
                      <div className="mono mt-0.5 flex items-center gap-2 text-[10.5px] text-ink-500">
                        <span>{(item.doc_type ?? "unknown").replace(/_/g, " ")}</span>
                        <span>·</span>
                        <span className="truncate">{item.file_name ?? "no file name"}</span>
                      </div>
                    </td>
                    <td>
                      <div className="mono text-[11px] text-teal-300/90">{reason.code}</div>
                      <div className="max-w-[320px] text-[12px] leading-snug text-ink-300">
                        {reason.message}
                      </div>
                      {item.flagged_fields.length > 0 ? (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {item.flagged_fields.slice(0, 3).map((field) => (
                            <span
                              key={field}
                              className="rounded bg-secondary px-1.5 py-0.5 text-[10.5px] text-ink-400"
                            >
                              {fieldLabel(field)}
                            </span>
                          ))}
                        </div>
                      ) : null}
                    </td>
                    <td className="mono text-right text-[12.5px] text-ink-200">
                      {money(item.amount_total)}
                      {item.requires_supervisor ? (
                        <div className="mt-1 text-[10px] text-amber-300">supervisor</div>
                      ) : null}
                    </td>
                    <td>
                      <ConfidenceBar value={item.overall_confidence} />
                    </td>
                    <td className="mono text-right text-[11.5px] text-ink-400">
                      {age(item.created_at)}
                      <div className="text-[10px] text-ink-600">{relative(item.created_at)}</div>
                    </td>
                    <td className="text-right">
                      <Link href={`/queue/${item.id}`} className="btn btn-sm btn-ghost">
                        Review <IconArrowRight size={13} />
                      </Link>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="flex items-center justify-between text-[12px] text-ink-400">
        <span className="mono">
          {offset + 1}–{offset + data.items.length}
        </span>
        <div className="flex gap-2">
          {offset > 0 ? (
            <Link className="btn btn-sm" href={href({ offset: String(Math.max(0, offset - PAGE_SIZE)) })}>
              Previous
            </Link>
          ) : null}
          {data.items.length === PAGE_SIZE ? (
            <Link className="btn btn-sm" href={href({ offset: String(offset + PAGE_SIZE) })}>
              Next
            </Link>
          ) : null}
        </div>
      </div>
    </div>
  );
}
