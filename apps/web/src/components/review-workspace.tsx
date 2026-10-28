"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  approveItem,
  holdItem,
  reassignItem,
  rejectItem,
  requestDocument,
} from "@/app/actions/review";
import { Badge, ConfidenceBar, OutcomeBadge } from "@/components/ui";
import {
  IconAlert,
  IconArrowRight,
  IconCheck,
  IconClock,
  IconFile,
  IconTally,
  IconX,
} from "@/components/icons";
import { confidence as toConfidence, dateOnly, dateTime, fieldLabel, money } from "@/lib/format";
import { highlightSpans } from "@/lib/highlight";
import type { FieldRow, ReviewItemDetail, RuleRow } from "@/lib/engine";

const REJECT_REASONS = [
  { value: "duplicate", label: "Duplicate" },
  { value: "not_a_business_document", label: "Not a business document" },
  { value: "wrong_tenant", label: "Wrong tenant" },
  { value: "illegible", label: "Illegible" },
  { value: "other", label: "Other" },
];

function valueOf(field: FieldRow): string {
  if (field.value_text) return field.value_text;
  if (field.value_num !== null && field.value_num !== undefined) return String(field.value_num);
  if (field.value_date) return field.value_date;
  if (field.value_json !== null && field.value_json !== undefined) return JSON.stringify(field.value_json);
  return "";
}

function toneFor(confidence: number): "ok" | "warn" | "bad" {
  if (confidence >= 0.93) return "ok";
  if (confidence >= 0.8) return "warn";
  return "bad";
}

function reasonTone(code: string): "bad" | "warn" {
  if (
    code.startsWith("rule_warned") ||
    code.startsWith("rule_degraded") ||
    code === "above_value_cap"
  ) {
    return "warn";
  }
  return "bad";
}

function ruleFields(rules: RuleRow[]): Set<string> {
  const set = new Set<string>();
  for (const rule of rules) {
    if (rule.outcome === "pass" || rule.outcome === "skipped") continue;
    for (const field of rule.fields ?? []) set.add(field);
  }
  return set;
}

export function ReviewWorkspace({
  detail,
  nextId,
  canAct,
  approvalLimit,
  confidenceThreshold,
}: {
  detail: ReviewItemDetail;
  nextId: string | null;
  canAct: boolean;
  approvalLimit: number;
  confidenceThreshold: number;
}) {
  const router = useRouter();
  const { item, record, rules, document: doc, pages, voucher_preview: preview, voucher } = detail;

  const failedFields = useMemo(() => ruleFields(rules), [rules]);
  const flagged = useMemo(() => new Set(item.flagged_fields ?? []), [item.flagged_fields]);

  const orderedFields = useMemo(() => {
    const isPriority = (field: FieldRow) =>
      flagged.has(field.path) || failedFields.has(field.path) || toConfidence(field.confidence) < confidenceThreshold;
    return [...detail.fields].sort((a, b) => {
      const ap = isPriority(a) ? 0 : 1;
      const bp = isPriority(b) ? 0 : 1;
      if (ap !== bp) return ap - bp;
      if (a.mandatory !== b.mandatory) return a.mandatory ? -1 : 1;
      return toConfidence(a.confidence) - toConfidence(b.confidence);
    });
  }, [detail.fields, flagged, failedFields, confidenceThreshold]);

  const [edits, setEdits] = useState<Record<string, string>>({});
  const [oneOff, setOneOff] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<string | null>(orderedFields[0]?.path ?? null);
  const [page, setPage] = useState(0);
  const [note, setNote] = useState("");
  const [panel, setPanel] = useState<"none" | "reject" | "hold" | "request" | "reassign">("none");
  const [rejectReason, setRejectReason] = useState("duplicate");
  const [channel, setChannel] = useState("email");
  const [assignee, setAssignee] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRefs = useRef<Record<string, HTMLInputElement | null>>({});

  const activePage = pages[page];
  const pageText = activePage?.text_digest ?? "";

  const spans = useMemo(() => {
    const lines = pageText.split(/\r?\n/);
    return detail.fields
      .filter((field) => field.source_span && (field.page_no ?? 1) === (activePage?.page_no ?? 1))
      .map((field) => {
        const source = field.source_span as string;
        const tone = toneFor(toConfidence(field.confidence));
        const active = field.path === selected;
        // The extractor records where a value was found, e.g. "line 7: Invoice No"
        // or "line 14 table, row 2". Prefer highlighting that line; fall back to
        // the recorded text when the span already holds the value itself.
        const at = /line\s+(\d+)/i.exec(source);
        if (at) {
          const row = /row\s+(\d+)/i.exec(source);
          const index = Number(at[1]) + (row ? Number(row[1]) - 1 : 0) - 1;
          const lineText = lines[index]?.trim();
          if (lineText && lineText.length >= 2) {
            return { text: lineText, tone, active, field: field.path };
          }
        }
        return { text: source, tone, active, field: field.path };
      })
      .filter((span) => !/^(line|page)\s+\d+/i.test(span.text));
  }, [detail.fields, activePage?.page_no, selected, pageText]);

  const segments = useMemo(() => highlightSpans(pageText, spans), [pageText, spans]);

  const editedPaths = Object.keys(edits);
  const correctedCount = editedPaths.length;
  const overLimit = Number(record.amount_total ?? 0) > approvalLimit;
  const needsSupervisor = item.requires_supervisor || overLimit;

  function move(delta: number) {
    const index = orderedFields.findIndex((field) => field.path === selected);
    const next = orderedFields[Math.max(0, Math.min(orderedFields.length - 1, index + delta))];
    if (next) {
      setSelected(next.path);
      const field = detail.fields.find((f) => f.path === next.path);
      if (field?.page_no) setPage(Math.max(0, pages.findIndex((p) => p.page_no === field.page_no)));
      inputRefs.current[next.path]?.focus();
    }
  }

  async function run(task: () => Promise<{ ok: boolean; error?: string }>, advance = true) {
    setBusy(true);
    setError(null);
    const result = await task();
    setBusy(false);
    if (!result.ok) {
      setError(result.error ?? "That action failed.");
      return;
    }
    if (advance) {
      router.push(nextId ? `/queue/${nextId}` : "/queue");
      router.refresh();
    }
  }

  function approve() {
    if (!canAct) return;
    const payload: Record<string, string> = {};
    for (const path of editedPaths) payload[path] = edits[path];
    void run(() =>
      approveItem({
        itemId: item.id,
        note: note || undefined,
        edits: payload,
        oneOffFields: [...oneOff],
      }),
    );
  }

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) {
        if (event.key === "Escape") target.blur();
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === "j") move(1);
      else if (event.key === "k") move(-1);
      else if (event.key === "a") approve();
      else if (event.key === "r") setPanel("reject");
      else if (event.key === "h") setPanel("hold");
      else if (event.key === "Escape") setPanel("none");
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orderedFields, selected, edits, note, canAct, nextId]);

  const debits = preview?.voucher.lines.filter((line) => line.is_debit) ?? [];
  const credits = preview?.voucher.lines.filter((line) => !line.is_debit) ?? [];

  const draftMode = (item.reasons ?? []).some((reason) => reason.code === "mode_draft");
  const triggers = (item.reasons ?? []).filter(
    (reason) => reason.code !== "mode_draft" && reason.code !== "mode_shadow",
  );

  return (
    <div className="space-y-4 pb-24">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link href="/queue" className="text-[11.5px] text-ink-500 hover:text-ink-300">
            ← Review queue
          </Link>
          <h1 className="mt-1 truncate text-[21px] font-semibold tracking-tight text-ink-100">
            {record.vendor_name ?? "Unnamed vendor"}
          </h1>
          <div className="mono mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-400">
            <span className="text-teal-300">{detail.pack.doc_type_label ?? record.doc_type}</span>
            <span>{record.document_number ?? "no number"}</span>
            <span>{dateOnly(record.document_date)}</span>
            <span>{record.vendor_gstin ?? "no GSTIN"}</span>
            <span className="text-ink-200">{money(record.amount_total)}</span>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {item.priority_band === "rule_failure" ? (
            <Badge tone="bad">rule failure</Badge>
          ) : item.priority_band === "low_confidence" ? (
            <Badge tone="warn">low confidence</Badge>
          ) : (
            <Badge tone="muted">routine</Badge>
          )}
          {needsSupervisor ? <Badge tone="warn">needs supervisor</Badge> : null}
          <Badge tone="muted">{item.status.replace(/_/g, " ")}</Badge>
          {voucher?.status === "posted" ? <Badge tone="ok">posted to Tally</Badge> : null}
          <span className="hidden items-center gap-1.5 text-[11px] text-ink-500 md:flex">
            <IconClock size={12} /> j/k fields · a approve · r reject · h hold
          </span>
        </div>
      </div>

      {draftMode ? (
        <div className="rounded-xl border hairline bg-secondary px-3 py-2 text-[12px] text-ink-300">
          <strong className="font-medium text-ink-100">Draft mode.</strong> This tenant stages every
          voucher for a person to approve, so records arrive here even when nothing is wrong with them.
        </div>
      ) : null}

      {triggers.length > 0 ? (
        <ul className="flex flex-col gap-1.5">
          {triggers.map((reason, index) => (
            <li
              key={`${reason.code}-${index}`}
              className={`flex items-start gap-2.5 rounded-xl border px-3 py-2 ${
                reasonTone(reason.code) === "warn"
                  ? "border-amber-500/20 bg-amber-500/[0.05]"
                  : "border-rose-500/20 bg-rose-500/[0.06]"
              }`}
            >
              <IconAlert
                size={14}
                className={`mt-0.5 shrink-0 ${
                  reasonTone(reason.code) === "warn" ? "text-amber-300" : "text-rose-300"
                }`}
              />
              <div className="min-w-0">
                <span
                  className={`mono mr-2 text-[11px] ${
                    reasonTone(reason.code) === "warn" ? "text-amber-300" : "text-rose-300"
                  }`}
                >
                  {reason.code}
                </span>
                <span
                  className={`text-[12.5px] ${
                    reasonTone(reason.code) === "warn" ? "text-amber-100/90" : "text-rose-100/90"
                  }`}
                >
                  {reason.message}
                </span>
                {(reason.fields ?? []).length > 0 ? (
                  <span className="ml-2 text-[11px] text-ink-500">
                    {(reason.fields ?? []).map(fieldLabel).join(", ")}
                  </span>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      ) : null}

      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,0.92fr)_minmax(0,1.08fr)]">
        {/* Source document */}
        <section className="panel sticky top-[70px] overflow-hidden">
          <div className="flex items-center justify-between gap-2 border-b hairline px-3.5 py-2.5">
            <div className="flex min-w-0 items-center gap-2">
              <IconFile size={14} className="shrink-0 text-ink-500" />
              <span className="truncate text-[12.5px] text-ink-200">{doc.file_name ?? "document"}</span>
              <Badge tone="muted">{doc.intake_channel.replace(/_/g, " ")}</Badge>
            </div>
            {pages.length > 1 ? (
              <div className="flex items-center gap-1">
                {pages.map((p, index) => (
                  <button
                    key={p.page_no}
                    onClick={() => setPage(index)}
                    className={`mono rounded px-1.5 py-0.5 text-[11px] ${
                      index === page ? "bg-ink-700 text-ink-100" : "text-ink-500 hover:text-ink-200"
                    }`}
                  >
                    {p.page_no}
                  </button>
                ))}
              </div>
            ) : null}
          </div>

          {pageText ? (
            <pre className="max-h-[calc(100vh-190px)] overflow-auto whitespace-pre-wrap bg-[#f7f4ec] px-4 py-3.5 font-mono text-[11.5px] leading-[1.75] text-[#21201b]">
              {segments.map((segment, index) =>
                segment.tone ? (
                  <mark
                    key={index}
                    className="doc-span"
                    data-tone={segment.active ? undefined : segment.tone}
                    onMouseEnter={() => segment.field && setSelected(segment.field)}
                    title={segment.field ? fieldLabel(segment.field) : undefined}
                  >
                    {segment.text}
                  </mark>
                ) : (
                  <span key={index}>{segment.text}</span>
                ),
              )}
            </pre>
          ) : (
            <div className="px-4 py-10 text-center">
              <p className="text-[12.5px] text-ink-300">This page has no text layer.</p>
              <p className="mx-auto mt-1.5 max-w-sm text-[11.5px] leading-relaxed text-ink-500">
                The file was read as an image, so there is no page text to highlight. Field positions
                below still carry the model&apos;s own confidence; the original is kept in tenant
                storage and never sent to the bridge agent.
              </p>
            </div>
          )}

          <div className="flex items-center justify-between border-t hairline px-3.5 py-2 text-[10.5px] text-ink-500">
            <span>
              {doc.sender ? `from ${doc.sender}` : "sender unknown"} ·{" "}
              {dateTime(doc.received_at)}
            </span>
            <span className="mono">
              {spans.length} located field{spans.length === 1 ? "" : "s"}
            </span>
          </div>
        </section>

        {/* Extracted fields, rules, voucher */}
        <div className="space-y-4">
          <section className="panel overflow-hidden">
            <div className="flex items-center justify-between border-b hairline px-3.5 py-2.5">
              <div className="label-xs">Extracted fields</div>
              <div className="flex items-center gap-2 text-[11px] text-ink-500">
                <span>{orderedFields.length} fields</span>
                {correctedCount > 0 ? <Badge tone="info">{correctedCount} changed</Badge> : null}
              </div>
            </div>
            <div className="max-h-[46vh] overflow-auto">
              {orderedFields.map((field) => {
                const current = edits[field.path] ?? valueOf(field);
                const changed = field.path in edits && edits[field.path] !== valueOf(field);
                const isFlagged =
                  flagged.has(field.path) ||
                  failedFields.has(field.path) ||
                  toConfidence(field.confidence) < confidenceThreshold;
                return (
                  <div
                    key={field.path}
                    className="field-row grid grid-cols-[minmax(0,1fr)_auto] items-start gap-2 border-b border-border px-3.5 py-2"
                    data-flagged={isFlagged}
                    data-selected={selected === field.path}
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5">
                        <span className="truncate text-[11.5px] text-ink-300">
                          {field.label ?? fieldLabel(field.path)}
                        </span>
                        {field.mandatory ? <span className="text-[10px] text-rose-400">required</span> : null}
                        {field.corrected ? <span className="text-[10px] text-teal-300">learned</span> : null}
                      </div>
                      <input
                        ref={(node) => {
                          inputRefs.current[field.path] = node;
                        }}
                        className={`input mono mt-1 !py-1.5 text-[12px] ${
                          changed ? "!border-teal-400/60" : ""
                        }`}
                        value={current}
                        onFocus={() => {
                          setSelected(field.path);
                          if (field.page_no) {
                            const index = pages.findIndex((p) => p.page_no === field.page_no);
                            if (index >= 0) setPage(index);
                          }
                        }}
                        onChange={(event) =>
                          setEdits((prev) => ({ ...prev, [field.path]: event.target.value }))
                        }
                      />
                      {changed ? (
                        <div className="mt-1 flex items-center gap-2 text-[10.5px]">
                          <label className="flex items-center gap-1 text-ink-400">
                            <input
                              type="checkbox"
                              className="accent-teal-400"
                              checked={oneOff.has(field.path)}
                              onChange={(event) =>
                                setOneOff((prev) => {
                                  const nextSet = new Set(prev);
                                  if (event.target.checked) nextSet.add(field.path);
                                  else nextSet.delete(field.path);
                                  return nextSet;
                                })
                              }
                            />
                            one-off, do not learn
                          </label>
                          <button
                            className="text-ink-500 hover:text-ink-200"
                            onClick={() =>
                              setEdits((prev) => {
                                const nextEdits = { ...prev };
                                delete nextEdits[field.path];
                                return nextEdits;
                              })
                            }
                          >
                            revert
                          </button>
                        </div>
                      ) : null}
                    </div>
                    <div className="flex flex-col items-end gap-1 pt-4">
                      <ConfidenceBar value={field.confidence} width={34} />
                      <span className="mono text-[9.5px] text-ink-600">
                        {field.page_no ? `p${field.page_no}` : "—"}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          </section>

          <section className="panel overflow-hidden">
            <div className="flex items-center justify-between border-b hairline px-3.5 py-2.5">
              <div className="label-xs">Rules</div>
              <div className="flex items-center gap-2">
                {rules.filter((rule) => rule.outcome === "fail").length > 0 ? (
                  <Badge tone="bad">{rules.filter((r) => r.outcome === "fail").length} failed</Badge>
                ) : null}
                {rules.filter((rule) => rule.outcome === "warn").length > 0 ? (
                  <Badge tone="warn">{rules.filter((r) => r.outcome === "warn").length} warnings</Badge>
                ) : null}
                <span className="text-[11px] text-ink-500">pack v{detail.pack.version}</span>
              </div>
            </div>
            <ul className="max-h-[28vh] divide-y divide-white/5 overflow-auto">
              {rules.map((rule) => (
                <li key={rule.rule_key} className="px-3.5 py-2">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-[12px] text-ink-200">{rule.title ?? fieldLabel(rule.rule_key)}</span>
                        {rule.degraded ? <Badge tone="info">lookup unavailable</Badge> : null}
                      </div>
                      {rule.message ? (
                        <p className="mt-0.5 text-[11.5px] leading-snug text-ink-400">{rule.message}</p>
                      ) : null}
                      {(rule.fields ?? []).length > 0 ? (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {rule.fields.map((field) => (
                            <button
                              key={field}
                              onClick={() => setSelected(field)}
                              className="rounded bg-secondary px-1.5 py-0.5 text-[10px] text-ink-400 hover:text-ink-200"
                            >
                              {fieldLabel(field)}
                            </button>
                          ))}
                        </div>
                      ) : null}
                    </div>
                    <OutcomeBadge outcome={rule.outcome} />
                  </div>
                </li>
              ))}
              {rules.length === 0 ? (
                <li className="px-3.5 py-4 text-[12px] text-ink-500">No rule results stored.</li>
              ) : null}
            </ul>
          </section>

          <section className="panel overflow-hidden">
            <div className="flex items-center justify-between border-b hairline px-3.5 py-2.5">
              <div className="flex items-center gap-2">
                <IconTally size={14} className="text-ink-500" />
                <span className="label-xs">Tally voucher preview</span>
              </div>
              {preview ? (
                preview.balanced ? (
                  <Badge tone="ok">balanced</Badge>
                ) : (
                  <Badge tone="bad">does not balance</Badge>
                )
              ) : (
                <Badge tone="muted">no preview</Badge>
              )}
            </div>

            {preview ? (
              <div className="px-3.5 py-3">
                <div className="mono flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-400">
                  <span className="text-ink-200">{preview.voucher.voucher_type}</span>
                  <span>{dateOnly(preview.voucher.voucher_date)}</span>
                  <span>{preview.voucher.party_ledger ?? "no party ledger"}</span>
                  {preview.voucher.place_of_supply ? (
                    <span>POS {preview.voucher.place_of_supply}</span>
                  ) : null}
                </div>

                <table className="mt-2.5 w-full text-[11.5px]">
                  <thead>
                    <tr className="text-ink-500">
                      <th className="pb-1 text-left font-medium">Ledger / item</th>
                      <th className="pb-1 text-right font-medium">Debit</th>
                      <th className="pb-1 text-right font-medium">Credit</th>
                    </tr>
                  </thead>
                  <tbody className="mono">
                    {debits.map((line, index) => (
                      <tr key={`d-${index}`} className="border-t border-border">
                        <td className="py-1 pr-2 text-ink-200">
                          {line.ledger ?? line.stock_item ?? "—"}
                          {line.hsn ? <span className="ml-1 text-ink-600">{line.hsn}</span> : null}
                        </td>
                        <td className="py-1 text-right text-ink-100">{money(line.amount)}</td>
                        <td />
                      </tr>
                    ))}
                    {credits.map((line, index) => (
                      <tr key={`c-${index}`} className="border-t border-border">
                        <td className="py-1 pr-2 text-ink-200">{line.ledger ?? line.stock_item ?? "—"}</td>
                        <td />
                        <td className="py-1 text-right text-ink-100">{money(line.amount)}</td>
                      </tr>
                    ))}
                  </tbody>
                  <tfoot className="mono border-t border-border">
                    <tr>
                      <td className="py-1.5 text-ink-400">
                        GST {money(preview.voucher.gst_cgst)} CGST · {money(preview.voucher.gst_sgst)} SGST ·{" "}
                        {money(preview.voucher.gst_igst)} IGST
                      </td>
                      <td className="py-1.5 text-right text-ink-100" colSpan={2}>
                        {money(preview.voucher.amount_total)}
                      </td>
                    </tr>
                  </tfoot>
                </table>

                <p className="mono mt-2 truncate text-[10.5px] text-ink-600">
                  {preview.voucher.narration}
                </p>
                <p className="mono mt-0.5 truncate text-[10px] text-ink-600">
                  idempotency {preview.voucher.idempotency_key}
                </p>

                {preview.warnings.length > 0 ? (
                  <ul className="mt-2 space-y-1">
                    {preview.warnings.map((warning, index) => (
                      <li key={index} className="text-[11.5px] text-amber-200/80">
                        • {warning}
                      </li>
                    ))}
                  </ul>
                ) : null}

                {preview.proposals.length > 0 ? (
                  <p className="mt-2 text-[11.5px] leading-relaxed text-ink-400">
                    This voucher needs {preview.proposals.length} master data decision(s). New ledgers
                    are proposed for approval, never created silently.
                  </p>
                ) : null}
              </div>
            ) : (
              <p className="px-3.5 py-4 text-[12px] text-ink-500">
                No voucher can be built from this record until its fields are corrected.
              </p>
            )}
          </section>

          {detail.actions.length > 0 ? (
            <section className="panel overflow-hidden">
              <div className="border-b hairline px-3.5 py-2.5">
                <div className="label-xs">History</div>
              </div>
              <ul className="divide-y divide-white/5">
                {detail.actions.map((action, index) => (
                  <li key={index} className="flex items-start justify-between gap-3 px-3.5 py-2">
                    <div className="min-w-0">
                      <span className="text-[12px] text-ink-200">{action.action.replace(/_/g, " ")}</span>
                      {action.field_path ? (
                        <span className="mono ml-2 text-[10.5px] text-ink-500">
                          {action.field_path} → {action.after_value}
                        </span>
                      ) : null}
                      {action.note ? (
                        <p className="text-[11px] text-ink-400">{action.note}</p>
                      ) : null}
                    </div>
                    <span className="mono shrink-0 text-[10.5px] text-ink-600">
                      {dateTime(action.created_at)}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
      </div>

      {/* Decision bar */}
      <div className="fixed inset-x-0 bottom-0 z-30 border-t hairline bg-ink-950/92 backdrop-blur-md">
        <div className="mx-auto flex max-w-[1500px] flex-wrap items-center gap-2 px-5 py-3 lg:px-8">
          {error ? (
            <span className="flex items-center gap-1.5 text-[12px] text-rose-300">
              <IconAlert size={13} /> {error}
            </span>
          ) : (
            <span className="hidden text-[11.5px] text-ink-500 md:block">
              {canAct
                ? "Corrections are stored for this tenant's learning loop only."
                : "Your role can review but not approve."}
            </span>
          )}

          <input
            className="input ml-auto w-[240px]"
            placeholder="Reviewer note (optional)"
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />

          <button className="btn btn-sm" disabled={busy || !canAct} onClick={() => setPanel("hold")}>
            Hold
          </button>
          <button className="btn btn-sm" disabled={busy || !canAct} onClick={() => setPanel("request")}>
            Request document
          </button>
          <button className="btn btn-sm" disabled={busy || !canAct} onClick={() => setPanel("reassign")}>
            Reassign
          </button>
          <button className="btn btn-sm btn-danger" disabled={busy || !canAct} onClick={() => setPanel("reject")}>
            <IconX size={13} /> Reject
          </button>
          <button className="btn btn-primary" disabled={busy || !canAct} onClick={approve}>
            <IconCheck size={14} />
            {busy
              ? "Working…"
              : correctedCount > 0
                ? `Approve with ${correctedCount} correction${correctedCount === 1 ? "" : "s"}`
                : "Approve & post"}
            <IconArrowRight size={13} />
          </button>
        </div>

        {panel !== "none" ? (
          <div className="border-t hairline bg-ink-900/95 px-5 py-3 lg:px-8">
            <div className="mx-auto flex max-w-[1500px] flex-wrap items-end gap-3">
              {panel === "reject" ? (
                <>
                  <label className="block">
                    <span className="label-xs">Reason (required)</span>
                    <select
                      className="select mt-1 w-[230px]"
                      value={rejectReason}
                      onChange={(event) => setRejectReason(event.target.value)}
                    >
                      {REJECT_REASONS.map((reason) => (
                        <option key={reason.value} value={reason.value}>
                          {reason.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    className="btn btn-danger"
                    disabled={busy}
                    onClick={() =>
                      void run(() =>
                        rejectItem({ itemId: item.id, reason: rejectReason, note: note || undefined }),
                      )
                    }
                  >
                    Reject record
                  </button>
                </>
              ) : null}

              {panel === "hold" ? (
                <>
                  <label className="block flex-1">
                    <span className="label-xs">Held because</span>
                    <input
                      className="input mt-1"
                      placeholder="Waiting for the missing purchase order"
                      value={note}
                      onChange={(event) => setNote(event.target.value)}
                    />
                  </label>
                  <button
                    className="btn"
                    disabled={busy || !note.trim()}
                    onClick={() => void run(() => holdItem({ itemId: item.id, note }), false)}
                  >
                    Hold record
                  </button>
                </>
              ) : null}

              {panel === "request" ? (
                <>
                  <label className="block">
                    <span className="label-xs">Channel</span>
                    <select
                      className="select mt-1 w-[130px]"
                      value={channel}
                      onChange={(event) => setChannel(event.target.value)}
                    >
                      <option value="email">Email</option>
                      <option value="whatsapp">WhatsApp</option>
                    </select>
                  </label>
                  <label className="block flex-1">
                    <span className="label-xs">Message</span>
                    <input
                      className="input mt-1"
                      placeholder="Please send a clearer copy of page 1"
                      value={note}
                      onChange={(event) => setNote(event.target.value)}
                    />
                  </label>
                  <button
                    className="btn"
                    disabled={busy}
                    onClick={() =>
                      void run(
                        () =>
                          requestDocument({
                            itemId: item.id,
                            channel,
                            templateKey: "clearer_copy",
                            message: note || undefined,
                          }),
                        false,
                      )
                    }
                  >
                    Send request
                  </button>
                </>
              ) : null}

              {panel === "reassign" ? (
                <>
                  <label className="block flex-1">
                    <span className="label-xs">Assign to</span>
                    <input
                      className="input mt-1"
                      placeholder="priya@firm.example or supervisor"
                      value={assignee}
                      onChange={(event) => setAssignee(event.target.value)}
                    />
                  </label>
                  <button
                    className="btn"
                    disabled={busy || !assignee.trim()}
                    onClick={() =>
                      void run(
                        () => reassignItem({ itemId: item.id, assignee, note: note || undefined }),
                        false,
                      )
                    }
                  >
                    Reassign
                  </button>
                </>
              ) : null}

              <button className="btn btn-ghost" onClick={() => setPanel("none")}>
                Cancel
              </button>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
