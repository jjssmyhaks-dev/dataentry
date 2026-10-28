import {
  saveLimits,
  setAutonomyStage,
  setKillSwitch,
  setPostingMode,
  setRuleSetting,
} from "@/app/actions/settings";
import { Badge, Field, PageHeader, Problem, SectionTitle, Stat } from "@/components/ui";
import { IconAlert, IconShield } from "@/components/icons";
import {
  engineGet,
  EngineError,
  type AutonomyState,
  type RuleSettingRow,
} from "@/lib/engine";
import { atLeast, currentSession } from "@/lib/session";
import { money, number, percent } from "@/lib/format";

export const dynamic = "force-dynamic";

const STAGE_NAMES = [
  "0 · Foundations",
  "1 · Shadow mode",
  "2 · Autonomous on low-risk",
  "3 · Autonomous on standard",
  "4 · Full autonomy with monitoring",
];

interface PackSummary {
  key: string;
  name: string;
  version: string;
  status: string;
  checksum: string;
  documents: { key: string; label: string; mandatory: string[]; posts_to: string }[];
  rules: { key: string; title: string; severity: string; applies_to: string[] }[];
  targets: string[];
  evaluation_cases: number;
}

export default async function SettingsPage({
  searchParams,
}: {
  searchParams: Promise<{ ok?: string; error?: string }>;
}) {
  const session = await currentSession();
  if (!session) return null;
  const params = await searchParams;

  let autonomy: AutonomyState | null = null;
  let rules: RuleSettingRow[] = [];
  let pack: { active: PackSummary } | null = null;
  let failure: EngineError | null = null;

  try {
    const [a, r, p] = await Promise.all([
      engineGet<AutonomyState>("/v1/autonomy", session),
      engineGet<{ rules: RuleSettingRow[] }>("/v1/settings/rules", session),
      engineGet<{ active: PackSummary }>("/v1/packs", session),
    ]);
    autonomy = a;
    rules = r.rules;
    pack = p;
  } catch (error) {
    if (error instanceof EngineError) failure = error;
    else throw error;
  }

  if (failure || !autonomy || !pack) {
    return (
      <div className="space-y-6">
        <PageHeader eyebrow="Admin" title="Autonomy & settings" />
        <Problem
          title={failure?.isOffline ? "The engine is not running" : "Could not load settings"}
          detail={failure?.detail ?? "No response from the engine."}
        />
      </div>
    );
  }

  const isOwner = atLeast(session.role, "owner");
  const limits = (autonomy as unknown as { limits?: Record<string, number | boolean> }).limits ?? {};
  const confidenceLimit = Number(limits.confidence_threshold ?? 0.93);
  const valueCap = Number(limits.autopost_value_cap ?? 50000);
  const dailyCap = Number(limits.daily_autopost_cap ?? 200);
  const errorLimit = Number(limits.error_rate_limit ?? 0.005);
  const autoDemote = Boolean(limits.auto_demote_on_breach ?? true);

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Admin"
        title="Autonomy & settings"
        description="Autonomy is granted per tenant and per stage. A stage opens only when its gate is measured and passed — the numbers here are proposals from section 12a, tuned with a design partner, never hard-coded."
        actions={
          <>
            <Badge tone={autonomy.posting_mode === "autonomous" ? "ok" : "warn"}>
              {autonomy.posting_mode} mode
            </Badge>
            {autonomy.kill_switch ? <Badge tone="bad">kill switch engaged</Badge> : null}
          </>
        }
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

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          label="Error rate"
          value={percent(autonomy.error_rate, 2)}
          tone={autonomy.error_rate > errorLimit ? "bad" : "ok"}
          hint={`Limit ${percent(errorLimit, 2)} · unreversed errors per auto-posted voucher`}
        />
        <Stat
          label="Auto-post value cap"
          value={money(valueCap, { compact: true })}
          hint="Anything above this goes to a reviewer"
        />
        <Stat
          label="Daily auto-post cap"
          value={number(dailyCap)}
          hint={`${number(autonomy.metrics.documents_processed ?? 0)} documents read in the window`}
        />
        <Stat
          label="Confidence threshold"
          value={percent(confidenceLimit, 1)}
          hint="Every field must clear this to post untouched"
        />
      </section>

      <div className="grid gap-4 xl:grid-cols-[1fr_1fr]">
        <section className="panel-flat overflow-hidden">
          <div className="border-b hairline px-4 py-3">
            <SectionTitle>Stage & gate</SectionTitle>
          </div>
          <div className="space-y-4 px-4 py-4">
            <p className="text-[12.5px] leading-relaxed text-ink-300">{autonomy.stage.description}</p>

            <ul className="space-y-2">
              {autonomy.gate.checks.map((check) => (
                <li key={check.key} className="flex items-start justify-between gap-3 text-[12px]">
                  <span className="text-ink-300">{check.label}</span>
                  <span className={`mono shrink-0 ${check.passed ? "text-emerald-300" : "text-ink-400"}`}>
                    {check.actual === null ? "no data" : Number(check.actual).toLocaleString("en-IN")}
                    <span className="text-ink-600"> / {check.comparison} {check.target}</span>
                  </span>
                </li>
              ))}
            </ul>

            <form action={setAutonomyStage} className="space-y-2 border-t hairline pt-3">
              <Field label="Move this tenant to">
                <select className="select" name="stage" defaultValue={autonomy.stage.stage}>
                  {STAGE_NAMES.map((name, index) => (
                    <option key={name} value={index}>
                      {name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Reason (recorded in the audit trail)">
                <input className="input" name="reason" placeholder="Design partner signed off the gate" />
              </Field>
              <button className="btn" disabled={!isOwner}>
                Set stage
              </button>
              {!isOwner ? (
                <p className="text-[11px] text-ink-500">Only an owner can change autonomy.</p>
              ) : null}
            </form>

            <div className="flex items-start gap-2 rounded-xl border hairline bg-secondary px-3 py-2 text-[11.5px] leading-relaxed text-ink-400">
              <IconShield size={14} className="mt-0.5 shrink-0 text-teal-300" />
              <span>
                A stage gate is measured from real postings, not field accuracy. If the error rate
                crosses the limit, the engine demotes the tenant to draft mode on its own
                {autoDemote ? "" : " (automatic demotion is currently switched off)"}.
              </span>
            </div>
          </div>
        </section>

        <div className="space-y-4">
          <section className="panel-flat overflow-hidden">
            <div className="border-b hairline px-4 py-3">
              <SectionTitle>Posting mode</SectionTitle>
            </div>
            <div className="space-y-3 px-4 py-4">
              {[
                {
                  value: "autonomous",
                  title: "Autonomous",
                  body: "Records that pass every rule and confidence gate post to Tally directly. Only flagged records reach the queue.",
                },
                {
                  value: "draft",
                  title: "Draft",
                  body: "Every voucher is staged for a human to approve before it posts. The safe fallback while trust is being earned.",
                },
                {
                  value: "shadow",
                  title: "Shadow",
                  body: "Processes real documents and records what it would have posted. Humans still do the entry; nothing is posted.",
                },
              ].map((option) => (
                <form key={option.value} action={setPostingMode} className="flex items-start gap-3">
                  <input type="hidden" name="mode" value={option.value} />
                  <input type="hidden" name="reason" value="changed in posting mode panel" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-[12.5px] font-medium text-ink-100">{option.title}</span>
                      {autonomy.posting_mode === option.value ? <Badge tone="ok">active</Badge> : null}
                    </div>
                    <p className="mt-0.5 text-[11.5px] leading-relaxed text-ink-400">{option.body}</p>
                  </div>
                  <button
                    className="btn btn-sm"
                    disabled={!isOwner || autonomy.posting_mode === option.value}
                  >
                    Use
                  </button>
                </form>
              ))}

              <form action={setKillSwitch} className="flex items-center gap-3 border-t hairline pt-3">
                <input type="hidden" name="engaged" value={autonomy.kill_switch ? "false" : "true"} />
                <div className="min-w-0 flex-1">
                  <div className="text-[12.5px] font-medium text-ink-100">Kill switch</div>
                  <p className="text-[11.5px] text-ink-400">
                    Moves the tenant to draft mode immediately. Nothing is lost; queued vouchers wait.
                  </p>
                </div>
                <button className={`btn btn-sm ${autonomy.kill_switch ? "" : "btn-danger"}`} disabled={!isOwner}>
                  {autonomy.kill_switch ? "Release" : "Engage"}
                </button>
              </form>
            </div>
          </section>

          <section className="panel-flat overflow-hidden">
            <div className="border-b hairline px-4 py-3">
              <SectionTitle>Safety limits</SectionTitle>
            </div>
            <form action={saveLimits} className="space-y-3 px-4 py-4">
              <div className="grid grid-cols-2 gap-3">
                <Field label="Confidence threshold" hint="0.5 – 1.0">
                  <input
                    className="input mono"
                    name="confidence_threshold"
                    type="number"
                    step="0.01"
                    min="0.5"
                    max="1"
                    defaultValue={confidenceLimit}
                  />
                </Field>
                <Field label="Auto-post value cap (₹)">
                  <input
                    className="input mono"
                    name="autopost_value_cap"
                    type="number"
                    step="1000"
                    min="0"
                    defaultValue={valueCap}
                  />
                </Field>
                <Field label="Daily auto-post cap">
                  <input
                    className="input mono"
                    name="daily_autopost_cap"
                    type="number"
                    min="0"
                    defaultValue={dailyCap}
                  />
                </Field>
                <Field label="Error rate limit" hint="e.g. 0.005 = 0.5%">
                  <input
                    className="input mono"
                    name="error_rate_limit"
                    type="number"
                    step="0.001"
                    min="0"
                    max="0.5"
                    defaultValue={errorLimit}
                  />
                </Field>
              </div>
              <label className="flex items-center gap-2 text-[12px] text-ink-300">
                <input
                  type="checkbox"
                  name="auto_demote_on_breach"
                  defaultChecked={autoDemote}
                  className="accent-teal-400"
                />
                Demote to draft automatically when the error rate crosses the limit
              </label>
              <button className="btn" disabled={!isOwner}>
                Save limits
              </button>
            </form>
          </section>
        </div>
      </div>

      <section className="panel-flat overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b hairline px-4 py-3">
          <SectionTitle>Validation rules</SectionTitle>
          <div className="mono flex items-center gap-2 text-[11px] text-ink-500">
            <span className="text-teal-300">{pack.active.key}</span>
            <span>v{pack.active.version}</span>
            <span className="truncate">sha {pack.active.checksum.slice(0, 10)}</span>
            <Badge tone="ok">{pack.active.status}</Badge>
          </div>
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>Rule</th>
              <th>Applies to</th>
              <th>Severity</th>
              <th className="text-right">Enabled</th>
            </tr>
          </thead>
          <tbody>
            {pack.active.rules.map((rule) => {
              const setting = rules.find((row) => row.key === rule.key);
              return (
                <tr key={rule.key}>
                  <td>
                    <div className="text-[12.5px] text-ink-100">{rule.title}</div>
                    <div className="mono text-[10.5px] text-ink-500">{rule.key}</div>
                  </td>
                  <td className="text-[11.5px] text-ink-400">
                    {rule.applies_to.length > 0 ? rule.applies_to.join(", ").replace(/_/g, " ") : "all"}
                  </td>
                  <td>
                    <Badge tone={rule.severity === "fail" ? "bad" : "warn"}>{rule.severity}</Badge>
                  </td>
                  <td>
                    <form action={setRuleSetting} className="flex items-center justify-end gap-2">
                      <input type="hidden" name="rule_key" value={rule.key} />
                      <select
                        className="select w-[92px] !py-1 !text-[11.5px]"
                        name="severity_override"
                        defaultValue={setting?.severity_override ?? ""}
                      >
                        <option value="">pack default</option>
                        <option value="fail">fail</option>
                        <option value="warn">warn</option>
                      </select>
                      <label className="flex items-center gap-1.5 text-[11.5px] text-ink-400">
                        <input
                          type="checkbox"
                          name="enabled"
                          defaultChecked={setting?.enabled ?? true}
                          className="accent-teal-400"
                        />
                        on
                      </label>
                      <button className="btn btn-sm" disabled={!isOwner}>
                        Save
                      </button>
                    </form>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>

      <section className="panel-flat overflow-hidden">
        <div className="border-b hairline px-4 py-3">
          <SectionTitle right={<span className="mono text-[11px] text-ink-500">{pack.active.evaluation_cases} labelled cases</span>}>
            Active pack
          </SectionTitle>
        </div>
        <div className="grid gap-3 px-4 py-4 md:grid-cols-2 xl:grid-cols-3">
          {pack.active.documents.map((doc) => (
            <div key={doc.key} className="rounded-2xl border hairline bg-secondary px-3 py-2.5">
              <div className="text-[12.5px] font-medium text-ink-100">{doc.label}</div>
              <div className="mono mt-0.5 text-[10.5px] text-ink-500">posts to {doc.posts_to}</div>
              <div className="mt-1.5 flex flex-wrap gap-1">
                {doc.mandatory.slice(0, 5).map((field) => (
                  <span key={field} className="rounded bg-secondary px-1.5 py-0.5 text-[10px] text-ink-400">
                    {field.replace(/_/g, " ")}
                  </span>
                ))}
                {doc.mandatory.length > 5 ? (
                  <span className="text-[10px] text-ink-500">+{doc.mandatory.length - 5}</span>
                ) : null}
              </div>
            </div>
          ))}
        </div>
        <div className="border-t hairline px-4 py-3 text-[11.5px] text-ink-400">
          Connectors: <span className="mono text-ink-300">{pack.active.targets.join(", ")}</span>. Rules,
          tolerances and thresholds ship with the pack; a GST change is a pack update, not an engine
          release.
        </div>
      </section>

      <section className="panel-flat overflow-hidden">
        <div className="border-b hairline px-4 py-3">
          <SectionTitle>Roles</SectionTitle>
        </div>
        <div className="grid gap-3 px-4 py-4 md:grid-cols-3">
          {[
            ["Owner", "Billing, autonomy settings, the kill switch and user management."],
            ["Reviewer", "Works the queue: approve, correct, reject, hold and reassign."],
            ["Viewer", "Read-only access to activity and reports, for an auditor."],
          ].map(([role, body]) => (
            <div key={role} className="rounded-2xl border hairline bg-secondary px-3 py-2.5">
              <div className="flex items-center gap-2">
                <span className="text-[12.5px] font-medium text-ink-100">{role}</span>
                {session.role === role.toLowerCase() ? <Badge tone="ok">you</Badge> : null}
              </div>
              <p className="mt-1 text-[11.5px] leading-relaxed text-ink-400">{body}</p>
            </div>
          ))}
        </div>
        <p className="border-t hairline px-4 py-3 text-[11.5px] text-ink-400">
          Roles come from the WorkOS organisation in the signed session, never from the browser. The
          organisation resolves to one tenant and one Postgres schema; the schema is chosen on the
          server for every request.
        </p>
      </section>
    </div>
  );
}
