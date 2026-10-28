import type { ReactNode } from "react";
import { confidence as toConfidence } from "@/lib/format";

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-4 border-b pb-5 hairline">
      <div className="min-w-0">
        {eyebrow ? <div className="label-xs mb-1.5">{eyebrow}</div> : null}
        <h1 className="text-[22px] font-semibold tracking-tight text-ink-100">{title}</h1>
        {description ? (
          <p className="mt-1.5 max-w-2xl text-[13px] leading-relaxed text-ink-300">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone = "neutral",
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "neutral" | "ok" | "warn" | "bad" | "accent";
}) {
  const toneClass = {
    neutral: "text-ink-100",
    ok: "text-emerald-300",
    warn: "text-amber-300",
    bad: "text-rose-300",
    accent: "text-teal-300",
  }[tone];
  return (
    <div className="panel px-4 py-3.5">
      <div className="label-xs">{label}</div>
      <div className={`mono mt-1.5 text-[21px] leading-none font-semibold ${toneClass}`}>{value}</div>
      {hint ? <div className="mt-1.5 text-[11.5px] leading-snug text-ink-400">{hint}</div> : null}
    </div>
  );
}

export function Badge({
  tone = "muted",
  children,
}: {
  tone?: "ok" | "warn" | "bad" | "muted" | "info";
  children: ReactNode;
}) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function OutcomeBadge({ outcome }: { outcome: string }) {
  if (outcome === "pass") return <Badge tone="ok">pass</Badge>;
  if (outcome === "warn") return <Badge tone="warn">warn</Badge>;
  if (outcome === "fail") return <Badge tone="bad">fail</Badge>;
  return <Badge tone="muted">skipped</Badge>;
}

export function StatusBadge({ status }: { status: string }) {
  const tone =
    status === "posted" || status === "approved" || status === "auto_posted"
      ? "ok"
      : status === "rejected" || status === "exception" || status === "failed"
        ? "bad"
        : status === "held" || status === "clarification_requested" || status === "in_review"
          ? "warn"
          : "muted";
  return <Badge tone={tone}>{status.replace(/_/g, " ")}</Badge>;
}

export function ConfidenceBar({
  value,
  width = 46,
}: {
  value: string | number | null | undefined;
  width?: number;
}) {
  const c = toConfidence(value);
  const tone = c >= 0.93 ? "bg-emerald-400" : c >= 0.8 ? "bg-amber-400" : "bg-rose-400";
  return (
    <span className="inline-flex items-center gap-2">
      <span
        className="relative inline-block h-[3px] overflow-hidden rounded-full bg-ink-700"
        style={{ width }}
      >
        <span
          className={`absolute inset-y-0 left-0 ${tone}`}
          style={{ width: `${Math.max(3, Math.min(100, c * 100))}%` }}
        />
      </span>
      <span className="mono text-[11px] text-ink-300">{(c * 100).toFixed(0)}%</span>
    </span>
  );
}

export function Empty({
  title,
  children,
  icon,
}: {
  title: string;
  children?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-16 text-center">
      {icon ? <div className="text-ink-600">{icon}</div> : null}
      <div className="text-[14px] font-medium text-ink-200">{title}</div>
      {children ? <div className="max-w-md text-[12.5px] leading-relaxed text-ink-400">{children}</div> : null}
    </div>
  );
}

export function Problem({
  title,
  detail,
  hint,
}: {
  title: string;
  detail: string;
  hint?: ReactNode;
}) {
  return (
    <div className="panel border-rose-500/30 bg-rose-500/[0.06] p-5">
      <div className="text-[14px] font-semibold text-rose-200">{title}</div>
      <p className="mt-1.5 text-[12.5px] leading-relaxed text-rose-100/80">{detail}</p>
      {hint ? <div className="mt-3 text-[12px] leading-relaxed text-ink-300">{hint}</div> : null}
    </div>
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span className="label-xs">{label}</span>
      <div className="mt-1.5">{children}</div>
      {hint ? <span className="mt-1 block text-[11px] text-ink-400">{hint}</span> : null}
    </label>
  );
}

export function SectionTitle({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="mb-3 flex items-center justify-between gap-3">
      <h2 className="label-xs !text-ink-300">{children}</h2>
      {right}
    </div>
  );
}
