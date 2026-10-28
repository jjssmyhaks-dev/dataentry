"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { uploadIntake, type IntakeActionResult } from "@/app/actions/intake";
import { Badge, Field } from "@/components/ui";
import { IconAlert, IconCheck, IconUpload } from "@/components/icons";

export function IntakeForm() {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<IntakeActionResult | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [text, setText] = useState("");
  const formRef = useRef<HTMLFormElement>(null);

  async function submit(formData: FormData) {
    setBusy(true);
    setResult(null);
    const response = await uploadIntake(formData);
    setBusy(false);
    setResult(response);
    if (response.ok) {
      formRef.current?.reset();
      setFileName(null);
      setText("");
    }
  }

  const ingest = result?.result;

  return (
    <div className="grid gap-4 lg:grid-cols-[1.1fr_0.9fr]">
      <form ref={formRef} action={submit} className="panel space-y-3.5 p-4">
        <div>
          <div className="label-xs mb-1.5">Document</div>
          <label className="flex cursor-pointer flex-col items-center justify-center gap-2 rounded-2xl border border-dashed border-border bg-secondary px-4 py-8 text-center hover:border-teal-400/40 hover:bg-teal-400/[0.04]">
            <IconUpload size={20} className="text-ink-400" />
            <span className="text-[12.5px] text-ink-200">
              {fileName ?? "Choose a PDF, image, spreadsheet or bank statement export"}
            </span>
            <span className="text-[11px] text-ink-500">Up to 25 MB. Text, PDF and CSV work today.</span>
            <input
              type="file"
              name="file"
              className="hidden"
              accept=".pdf,.png,.jpg,.jpeg,.webp,.csv,.xlsx,.txt,.json"
              onChange={(event) => setFileName(event.target.files?.[0]?.name ?? null)}
            />
          </label>
        </div>

        <Field
          label="Or paste the document text"
          hint="Useful for WhatsApp forwards and email bodies. The classifier splits bundles automatically."
        >
          <textarea
            className="textarea mono h-28 text-[11.5px]"
            name="text"
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder={"TAX INVOICE\nVendor: ...\nGSTIN: ...\nInvoice No: ..."}
          />
        </Field>

        <div className="flex items-center gap-2">
          <select className="select w-[190px]" name="channel" defaultValue="web_upload">
            <option value="web_upload">Web upload</option>
            <option value="email">Email inbox</option>
            <option value="whatsapp">WhatsApp</option>
            <option value="bulk_upload">Bulk upload</option>
          </select>
          <button className="btn btn-primary ml-auto" disabled={busy}>
            {busy ? "Reading…" : "Read document"}
          </button>
        </div>
      </form>

      <div className="panel p-4">
        <div className="label-xs mb-2">What the engine did</div>
        {!result ? (
          <p className="text-[12.5px] leading-relaxed text-ink-400">
            A document is classified against the tenant&apos;s active pack, split if it holds several
            documents, extracted into the pack&apos;s schema, validated through the rule engine, then
            either posted or routed to the queue. The result appears here as it happens.
          </p>
        ) : !result.ok ? (
          <div className="flex items-start gap-2 rounded-xl border border-rose-500/25 bg-rose-500/[0.07] px-3 py-2 text-[12.5px] text-rose-200">
            <IconAlert size={14} className="mt-0.5 shrink-0" />
            <span>{result.error}</span>
          </div>
        ) : ingest ? (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={ingest.status === "exception" ? "bad" : "ok"}>{ingest.status}</Badge>
              <Badge tone="muted">{ingest.doc_type?.replace(/_/g, " ") ?? "unclassified"}</Badge>
              {ingest.segments > 1 ? <Badge tone="info">{ingest.segments} documents split</Badge> : null}
            </div>

            <dl className="grid grid-cols-2 gap-2 text-[12px]">
              <div className="rounded-xl bg-secondary px-2.5 py-1.5">
                <dt className="text-[10.5px] text-ink-500">Records extracted</dt>
                <dd className="mono text-ink-100">{ingest.records}</dd>
              </div>
              <div className="rounded-xl bg-secondary px-2.5 py-1.5">
                <dt className="text-[10.5px] text-ink-500">Posted autonomously</dt>
                <dd className="mono text-emerald-300">{ingest.posted_autonomously}</dd>
              </div>
              <div className="rounded-xl bg-secondary px-2.5 py-1.5">
                <dt className="text-[10.5px] text-ink-500">Sent to review</dt>
                <dd className="mono text-amber-300">{ingest.needs_review}</dd>
              </div>
              <div className="rounded-xl bg-secondary px-2.5 py-1.5">
                <dt className="text-[10.5px] text-ink-500">Vouchers queued</dt>
                <dd className="mono text-ink-100">{ingest.queued}</dd>
              </div>
            </dl>

            {ingest.duplicate_of ? (
              <p className="text-[12px] text-amber-200">
                This file matches an earlier document ({ingest.duplicate_of.slice(0, 8)}). It was not
                re-processed.
              </p>
            ) : null}

            {ingest.warnings.length > 0 ? (
              <ul className="space-y-1">
                {ingest.warnings.map((warning, index) => (
                  <li key={index} className="text-[11.5px] leading-snug text-ink-300">
                    • {warning}
                  </li>
                ))}
              </ul>
            ) : null}

            <div className="flex flex-wrap gap-2 pt-1">
              {ingest.review_item_ids[0] ? (
                <Link href={`/queue/${ingest.review_item_ids[0]}`} className="btn btn-sm btn-primary">
                  <IconCheck size={13} /> Open the review item
                </Link>
              ) : null}
              <Link href="/queue" className="btn btn-sm">
                Go to the queue
              </Link>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
