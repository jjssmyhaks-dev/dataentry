"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useTransition } from "react";
import { IconSearch } from "./icons";

const BANDS = [
  { value: "", label: "All bands" },
  { value: "rule_failure", label: "Rule failures" },
  { value: "low_confidence", label: "Low confidence" },
  { value: "routine", label: "Routine" },
];

const DOC_TYPES = [
  { value: "", label: "All types" },
  { value: "supplier_invoice", label: "Supplier invoice" },
  { value: "purchase_order", label: "Purchase order" },
  { value: "goods_receipt", label: "Goods receipt" },
  { value: "eway_bill", label: "E-way bill" },
  { value: "e_invoice", label: "E-invoice" },
  { value: "bank_statement", label: "Bank statement" },
];

const FLAG_CODES = [
  { value: "", label: "Any reason" },
  { value: "rule_failed", label: "Failed a rule" },
  { value: "critical_rule_failed", label: "Critical rule failed" },
  { value: "low_field_confidence", label: "Low field confidence" },
  { value: "rule_degraded", label: "Lookup unavailable" },
  { value: "above_value_cap", label: "Above value cap" },
  { value: "duplicate", label: "Possible duplicate" },
  { value: "tally_rejected", label: "Tally rejected it" },
];

export function QueueFilters({
  docType,
  band,
  reason,
  vendor,
  minAmount,
}: {
  docType: string;
  band: string;
  reason: string;
  vendor: string;
  minAmount: string;
}) {
  const router = useRouter();
  const params = useSearchParams();
  const [pending, startTransition] = useTransition();

  function update(key: string, value: string) {
    const next = new URLSearchParams(params.toString());
    if (value) next.set(key, value);
    else next.delete(key);
    next.delete("offset");
    startTransition(() => router.push(`/queue?${next.toString()}`));
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="relative">
        <IconSearch size={14} className="pointer-events-none absolute top-2.5 left-2.5 text-ink-500" />
        <input
          className="input w-[190px] !pl-8"
          placeholder="Vendor"
          defaultValue={vendor}
          onKeyDown={(event) => {
            if (event.key === "Enter") update("vendor", (event.target as HTMLInputElement).value);
          }}
          onBlur={(event) => {
            if (event.target.value !== vendor) update("vendor", event.target.value);
          }}
        />
      </div>

      <select className="select w-[150px]" value={docType} onChange={(e) => update("doc_type", e.target.value)}>
        {DOC_TYPES.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>

      <select className="select w-[150px]" value={band} onChange={(e) => update("band", e.target.value)}>
        {BANDS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>

      <select className="select w-[178px]" value={reason} onChange={(e) => update("reason", e.target.value)}>
        {FLAG_CODES.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>

      <select
        className="select w-[150px]"
        value={minAmount}
        onChange={(e) => update("min_amount", e.target.value)}
      >
        <option value="">Any value</option>
        <option value="10000">Above ₹10,000</option>
        <option value="100000">Above ₹1,00,000</option>
        <option value="500000">Above ₹5,00,000</option>
      </select>

      {pending ? <span className="text-[11.5px] text-ink-500">loading…</span> : null}
    </div>
  );
}
