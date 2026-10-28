import Link from "next/link";
import { Badge, ConfidenceBar, Empty, PageHeader, Problem, SectionTitle, Stat } from "@/components/ui";
import { IconAlert, IconArrowRight, IconBolt } from "@/components/icons";
import { engineGet, EngineError, type AutonomyState, type Dashboard } from "@/lib/engine";
import { currentSession } from "@/lib/session";
import { money, number, percent, relative } from "@/lib/format";

export const dynamic = "force-dynamic";

function num(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export default async function DashboardPage({
  searchParams,
}: {
  searchParams: Promise<{ days?: string }>;
}) {
  const session = await currentSession();
  if (!session) return null;
  const params = await searchParams;
  const days = [7, 30, 90].includes(Number(params.days)) ? Number(params.days) : 30;

  let dashboard: Dashboard | null = null;
  let autonomy: AutonomyState | null = null;
  let failure: EngineError | null = null;

  try {
    [dashboard, autonomy] = await Promise.all([
      engineGet<Dashboard>("/v1/dashboard", session, { days }),
      engineGet<AutonomyState>("/v1/autonomy", session),
    ]);
  } catch (error) {
    if (error instanceof EngineError) failure = error;
    else throw error;
  }

  if (failure || !dashboard || !autonomy) {
    return (
      <div className="space-y-6">
        <PageHeader
          eyebrow="Operations"
          title="Dashboard"
          description="Straight-through rate, the review backlog, and how close this tenant is to the next autonomy stage."
        />
        <Problem
          title={failure?.isOffline ? "The engine is not running" : "The engine refused this request"}
          detail={failure?.detail ?? "No response from the engine."}
          hint={
            <>
              Start it with <span className="mono">python -m engine.api.server</span>, then reload. The
              web app talks to the engine at <span className="mono">ENGINE_API_URL</span>; nothing in
              the browser touches tenant data directly.
            </>
          }
        />
      </div>
    );
  }

  const metrics = dashboard.metrics;
  const processed = num(metrics.documents_processed) ?? 0;
  const autoPosted = num(metrics.auto_posted) ?? 0;
  const inReview =
    (dashboard.queue.pending?.count ?? 0) +
    (dashboard.queue.held?.count ?? 0) +
    (dashboard.queue.clarification_requested?.count ?? 0);
  const straightThrough = processed > 0 ? autoPosted / processed : null;
  const errorRate = num(dashboard.error_rate) ?? 0;
  const gate = autonomy.gate;

  return (
    <div className="space-y-7">
      <PageHeader
        eyebrow="Operations"
        title="Dashboard"
        description={
          <>
            {dashboard.documents.documents} documents in the last {days} days across{" "}
            {number(dashboard.vendors.length)} vendors. Records that clear every rule and confidence
            gate post to Tally without a human; the rest arrive in the review queue.
          </>
        }
        actions={
          <div className="flex items-center gap-1 rounded-xl border hairline p-0.5">
            {[7, 30, 90].map((d) => (
              <Link
                key={d}
                href={`/?days=${d}`}
                className={`rounded-[7px] px-2.5 py-1 text-[12px] ${
                  d === days ? "bg-ink-700 text-ink-100" : "text-ink-400 hover:text-ink-200"
                }`}
              >
                {d}d
              </Link>
            ))}
          </div>
        }
      />

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <Stat
          label="Straight-through"
          value={percent(straightThrough)}
          tone={straightThrough !== null && straightThrough >= 0.6 ? "ok" : "neutral"}
          hint="Auto-posted with no human touch"
        />
        <Stat
          label="Waiting for review"
          value={number(inReview)}
          tone={inReview > 0 ? "warn" : "ok"}
          hint={`${money(
            (dashboard.queue.pending?.value ?? 0) +
              (dashboard.queue.held?.value ?? 0) +
              (dashboard.queue.clarification_requested?.value ?? 0),
            { compact: true },
          )} of invoices held`}
        />
        <Stat
          label="Auto-posted"
          value={number(autoPosted)}
          hint={`${number(dashboard.queued_vouchers.n)} vouchers queued for the agent`}
        />
        <Stat
          label="Error rate"
          value={percent(errorRate, 2)}
          tone={errorRate > 0.005 ? "bad" : "ok"}
          hint="Unreversed errors per auto-posted voucher"
        />
        <Stat
          label="Exceptions"
          value={number(dashboard.documents.exceptions)}
          tone={dashboard.documents.exceptions > 0 ? "bad" : "neutral"}
          hint="Unreadable or unsupported files"
        />
      </section>

      <div className="grid items-start gap-4 lg:grid-cols-[1.35fr_1fr]">
        <section className="panel-flat overflow-hidden">
          <div className="flex items-center justify-between border-b hairline px-4 py-3">
            <SectionTitle>Autonomy progress</SectionTitle>
            <Link href="/settings" className="btn btn-sm btn-ghost">
              Settings <IconArrowRight size={13} />
            </Link>
          </div>
          <div className="px-4 py-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={autonomy.posting_mode === "autonomous" ? "ok" : "warn"}>
                stage {autonomy.stage.stage} · {autonomy.stage.name}
              </Badge>
              <Badge tone={gate.passed ? "ok" : "muted"}>
                {gate.passed ? "gate to next stage passed" : "gate not yet passed"}
              </Badge>
              {autonomy.kill_switch ? <Badge tone="bad">kill switch engaged</Badge> : null}
            </div>
            <p className="mt-2.5 max-w-xl text-[12.5px] leading-relaxed text-ink-300">
              {autonomy.stage.description}
            </p>
            {autonomy.evaluation.source !== "real" ? (
              <p className="mt-2 max-w-xl rounded-xl border hairline bg-secondary px-2.5 py-1.5 text-[11.5px] leading-relaxed text-ink-400">
                {autonomy.evaluation.cases} labelled cases are in the pack, but they are{" "}
                <span className="text-amber-200">{autonomy.evaluation.source}</span>. The stage 0 gate
                stays closed until they are replaced with real design-partner documents.
              </p>
            ) : null}

            <ul className="mt-4 space-y-2.5">
              {gate.checks.map((check) => {
                const actual = check.actual;
                const pct =
                  actual === null
                    ? 0
                    : check.target === 0
                      ? 100
                      : Math.min(100, Math.max(3, (actual / check.target) * 100));
                return (
                  <li key={check.key}>
                    <div className="flex items-baseline justify-between gap-3 text-[12px]">
                      <span className="text-ink-200">{check.label}</span>
                      <span className={`mono ${check.passed ? "text-emerald-300" : "text-ink-400"}`}>
                        {actual === null ? "—" : Number(actual).toLocaleString("en-IN")}
                        <span className="text-ink-600"> / {check.comparison} {check.target}</span>
                      </span>
                    </div>
                    <div className="mt-1.5 h-[3px] overflow-hidden rounded-full bg-ink-700">
                      <div
                        className={`h-full ${check.passed ? "bg-emerald-400" : "bg-amber-400/70"}`}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  </li>
                );
              })}
            </ul>

            {autonomy.should_demote ? (
              <div className="mt-4 flex items-start gap-2 rounded-xl border border-rose-500/25 bg-rose-500/[0.07] px-3 py-2 text-[12px] text-rose-200">
                <IconAlert size={14} className="mt-0.5 shrink-0" />
                <span>
                  The engine would demote this tenant to draft mode: {autonomy.demote_reason}. The
                  kill switch moves it immediately; accuracy is tracked per document type and vendor.
                </span>
              </div>
            ) : null}
          </div>
        </section>

        <section className="panel-flat overflow-hidden">
          <div className="border-b hairline px-4 py-3">
            <SectionTitle right={<span className="badge badge-muted">{number(dashboard.top_flags.length)} codes</span>}>
              Why records get flagged
            </SectionTitle>
          </div>
          {dashboard.top_flags.length === 0 ? (
            <Empty title="No flags in this window" icon={<IconBolt size={22} />}>
              Nothing has been sent to review in the last {days} days.
            </Empty>
          ) : (
            <ul className="divide-y divide-white/5">
              {dashboard.top_flags.slice(0, 7).map((flag) => (
                <li key={`${flag.code}-${flag.message}`} className="flex items-start gap-3 px-4 py-2.5">
                  <span className="mono mt-0.5 w-7 shrink-0 text-right text-[11.5px] text-ink-400">
                    {flag.n}
                  </span>
                  <div className="min-w-0">
                    <div className="mono text-[11.5px] text-teal-300">{flag.code}</div>
                    <div className="truncate text-[12px] text-ink-300">{flag.message}</div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      <div className="grid items-start gap-4 lg:grid-cols-[1.35fr_1fr]">
        <section className="panel-flat overflow-hidden">
          <div className="flex items-center justify-between border-b hairline px-4 py-3">
            <SectionTitle>Waiting now</SectionTitle>
            <Link href="/queue" className="btn btn-sm btn-ghost">
              Open queue <IconArrowRight size={13} />
            </Link>
          </div>
          {inReview === 0 ? (
            <Empty title="Queue is clear" icon={<IconBolt size={22} />}>
              Every document in this window either posted itself or is waiting on nothing.
            </Empty>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Vendor</th>
                  <th>Type</th>
                  <th className="text-right">Amount</th>
                  <th>Band</th>
                  <th>Waiting</th>
                </tr>
              </thead>
              <tbody>
                {(["pending", "held", "clarification_requested"] as const).map((status) =>
                  dashboard.queue[status] ? (
                    <tr key={status}>
                      <td colSpan={5} className="bg-secondary py-2 text-[11.5px] text-ink-400">
                        {status.replace(/_/g, " ")} · {dashboard.queue[status].count} records ·{" "}
                        {money(dashboard.queue[status].value, { compact: true })}
                        {dashboard.queue[status].rule_failures > 0 ? (
                          <span className="ml-2 text-rose-300">
                            {dashboard.queue[status].rule_failures} rule failures
                          </span>
                        ) : null}
                      </td>
                    </tr>
                  ) : null,
                )}
              </tbody>
            </table>
          )}
        </section>

        <section className="panel-flat overflow-hidden">
          <div className="border-b hairline px-4 py-3">
            <SectionTitle>Vendor accuracy</SectionTitle>
          </div>
          {dashboard.vendors.length === 0 ? (
            <Empty title="No vendor history yet">Upload a document to start building it.</Empty>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Vendor</th>
                  <th className="text-right">Docs</th>
                  <th className="text-right">Auto</th>
                  <th>Mean confidence</th>
                </tr>
              </thead>
              <tbody>
                {dashboard.vendors.slice(0, 8).map((vendor) => (
                  <tr key={vendor.vendor_gstin}>
                    <td>
                      <div className="max-w-[160px] truncate text-[12.5px] text-ink-200">
                        {vendor.vendor_name ?? "Unknown vendor"}
                      </div>
                      <div className="mono text-[10.5px] text-ink-500">{vendor.vendor_gstin}</div>
                    </td>
                    <td className="mono text-right text-[12px] text-ink-300">{vendor.documents}</td>
                    <td className="mono text-right text-[12px] text-ink-300">{vendor.auto_posted}</td>
                    <td>
                      <ConfidenceBar value={vendor.mean_confidence} width={38} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>

      <section className="grid gap-3 sm:grid-cols-3">
        <Stat
          label="Queued for the bridge agent"
          value={number(dashboard.open_agent_commands)}
          hint="Signed commands waiting for a connected Tally PC"
        />
        <Stat
          label="Median review time"
          value={
            metrics.median_review_seconds === null || metrics.median_review_seconds === undefined
              ? "—"
              : `${Math.round(Number(metrics.median_review_seconds))}s`
          }
          hint="Target is under 30 seconds per record"
        />
        <Stat
          label="Approved without edits"
          value={percent(num(metrics.clean_review_share))}
          hint="Corrected share is what feeds the learning loop"
        />
      </section>

      <p className="text-[11.5px] text-ink-600">
        Window ends {relative(new Date().toISOString())}. Times are IST. Data comes from the engine,
        which reads this tenant&apos;s own schema.
      </p>
    </div>
  );
}
