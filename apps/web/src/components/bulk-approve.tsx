"use client";

import { useRouter } from "next/navigation";
import { Fragment, useMemo, useState } from "react";
import { bulkApprove } from "@/app/actions/review";
import { Badge, ConfidenceBar, Empty } from "@/components/ui";
import { IconAlert, IconCheck, IconInbox } from "@/components/icons";
import { money } from "@/lib/format";
import type { QueueRow } from "@/lib/engine";

export function BulkApprovePanel({
  items,
  canAct,
  approvalLimit,
}: {
  items: QueueRow[];
  canAct: boolean;
  approvalLimit: number;
}) {
  const router = useRouter();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [grouped, setGrouped] = useState(true);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  const rows = useMemo(() => {
    if (!grouped) return items;
    return [...items].sort((a, b) => (a.vendor_name ?? "").localeCompare(b.vendor_name ?? ""));
  }, [items, grouped]);

  const chosen = rows.filter((row) => selected.has(row.id));
  const total = chosen.reduce((sum, row) => sum + Number(row.amount_total ?? 0), 0);
  const overLimit = chosen.filter((row) => Number(row.amount_total ?? 0) > approvalLimit);
  const withWarnings = chosen.filter((row) => (row.warned_rules ?? []).length > 0);
  const blocked = chosen.some((row) => row.priority_band === "rule_failure" || row.requires_supervisor);

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    if (selected.size === rows.length) setSelected(new Set());
    else setSelected(new Set(rows.map((row) => row.id)));
  }

  async function submit() {
    setBusy(true);
    setError(null);
    const response = await bulkApprove({
      itemIds: [...selected],
      note: note || undefined,
      groupBy: grouped ? "vendor" : undefined,
    });
    setBusy(false);
    if (!response.ok) {
      setError(response.error ?? "Bulk approve failed.");
      return;
    }
    setSelected(new Set());
    setResult(`Approved ${chosen.length} record${chosen.length === 1 ? "" : "s"}.`);
    router.refresh();
  }

  if (items.length === 0) {
    return (
      <div className="panel">
        <Empty title="Nothing safe to bulk approve" icon={<IconInbox size={22} />}>
          Bulk approve only lists records that pass every rule and sit below the value cap. Anything
          that failed a rule must be opened individually.
        </Empty>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="panel flex flex-wrap items-center gap-3 px-4 py-3">
        <label className="flex items-center gap-2 text-[12.5px] text-ink-200">
          <input
            type="checkbox"
            className="accent-teal-400"
            checked={selected.size === rows.length && rows.length > 0}
            onChange={toggleAll}
          />
          Select all {rows.length}
        </label>
        <label className="flex items-center gap-2 text-[12px] text-ink-400">
          <input
            type="checkbox"
            className="accent-teal-400"
            checked={grouped}
            onChange={(event) => setGrouped(event.target.checked)}
          />
          Group by vendor
        </label>
        <div className="mono ml-auto flex items-center gap-4 text-[12px]">
          <span className="text-ink-300">{selected.size} selected</span>
          <span className="text-ink-100">{money(total, { compact: true })}</span>
        </div>
      </div>

      <div className="panel overflow-hidden">
        <table className="table">
          <thead>
            <tr>
              <th className="w-8" />
              <th>Vendor</th>
              <th>Reason it is queued</th>
              <th className="text-right">Amount</th>
              <th>Confidence</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => {
              const previous = rows[index - 1];
              const showGroup =
                grouped && (!previous || previous.vendor_name !== row.vendor_name);
              const requiresSupervisor = Number(row.amount_total ?? 0) > approvalLimit;
              const first = (row.reasons ?? [])[0];
              return (
                <Fragment key={row.id}>
                  {showGroup ? (
                    <tr>
                      <td colSpan={5} className="bg-secondary !py-1.5 text-[11px] text-ink-400">
                        {row.vendor_name ?? "Unnamed vendor"} ·{" "}
                        {rows.filter((r) => r.vendor_name === row.vendor_name).length} records
                      </td>
                    </tr>
                  ) : null}
                  <tr>
                    <td>
                      <input
                        type="checkbox"
                        className="accent-teal-400"
                        checked={selected.has(row.id)}
                        onChange={() => toggle(row.id)}
                      />
                    </td>
                    <td className="max-w-[220px] truncate text-[12.5px] text-ink-100">
                      {row.vendor_name ?? "Unnamed vendor"}
                      {requiresSupervisor ? (
                        <div className="text-[10px] text-amber-300">above the single-reviewer limit</div>
                      ) : null}
                    </td>
                    <td>
                      <div className="mono text-[10.5px] text-teal-300/80">{first?.code ?? "routine"}</div>
                      <div className="max-w-[360px] text-[12px] text-ink-300">
                        {!first || first.code === "mode_draft"
                          ? "Cleared every rule and confidence gate. It is only here because the tenant is in draft mode."
                          : first.message}
                      </div>
                      {(row.warned_rules ?? []).length > 0 ? (
                        <div className="mt-1">
                          <Badge tone="warn">{row.warned_rules.length} warning</Badge>
                        </div>
                      ) : null}
                    </td>
                    <td className="mono text-right text-[12.5px] text-ink-200">
                      {money(row.amount_total)}
                    </td>
                    <td>
                      <ConfidenceBar value={row.overall_confidence} />
                    </td>
                  </tr>
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="panel flex flex-wrap items-center gap-3 px-4 py-3">
        <input
          className="input w-[260px]"
          placeholder="Note for the audit trail"
          value={note}
          onChange={(event) => setNote(event.target.value)}
        />
        {overLimit.length > 0 ? (
          <span className="flex items-center gap-1.5 text-[12px] text-amber-300">
            <IconAlert size={13} /> {overLimit.length} above the approval limit
          </span>
        ) : null}
        {blocked ? (
          <span className="flex items-center gap-1.5 text-[12px] text-rose-300">
            <IconAlert size={13} /> Selection includes a rule failure; open those individually
          </span>
        ) : null}
        {withWarnings.length > 0 ? (
          <span className="text-[12px] text-amber-300">{withWarnings.length} carry warnings</span>
        ) : null}
        {error ? <span className="text-[12px] text-rose-300">{error}</span> : null}
        {result ? <span className="text-[12px] text-emerald-300">{result}</span> : null}
        <button
          className="btn btn-primary ml-auto"
          disabled={busy || !canAct || selected.size === 0 || blocked}
          onClick={submit}
        >
          <IconCheck size={14} />
          {busy ? "Approving…" : `Approve ${selected.size || ""} & post`}
        </button>
      </div>
    </div>
  );
}
