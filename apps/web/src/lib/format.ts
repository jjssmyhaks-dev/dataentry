export function money(value: string | number | null | undefined, opts?: { compact?: boolean }): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return "—";
  if (opts?.compact) {
    if (Math.abs(n) >= 1_00_00_000) return `₹${(n / 1_00_00_000).toFixed(2)} cr`;
    if (Math.abs(n) >= 1_00_000) return `₹${(n / 1_00_000).toFixed(2)} L`;
    if (Math.abs(n) >= 1_000) return `₹${(n / 1_000).toFixed(1)}k`;
  }
  return `₹${n.toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function number(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("en-IN");
}

export function percent(value: string | number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

export function confidence(value: string | number | null | undefined): number {
  if (value === null || value === undefined || value === "") return 0;
  const n = typeof value === "string" ? Number(value) : value;
  return Number.isFinite(n) ? n : 0;
}

export function confidenceTone(value: number): "ok" | "warn" | "bad" {
  if (value >= 0.93) return "ok";
  if (value >= 0.8) return "warn";
  return "bad";
}

const UTC = { timeZone: "Asia/Kolkata" } as const;

export function dateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-IN", {
    ...UTC,
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function dateOnly(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleDateString("en-IN", { ...UTC, day: "2-digit", month: "short", year: "numeric" });
}

export function relative(value: string | null | undefined): string {
  if (!value) return "—";
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return "—";
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days} d ago`;
  return dateOnly(value);
}

export function age(value: string | null | undefined): string {
  if (!value) return "—";
  const hours = (Date.now() - new Date(value).getTime()) / 3_600_000;
  if (!Number.isFinite(hours)) return "—";
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))}m`;
  if (hours < 24) return `${Math.round(hours)}h`;
  return `${Math.round(hours / 24)}d`;
}

export function label(value: string | null | undefined): string {
  if (!value) return "—";
  return value.replace(/[_.]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function fieldLabel(path: string): string {
  const parts = path.split(".");
  const last = parts[parts.length - 1];
  if (/^\d+$/.test(last) && parts.length > 1) {
    return `${label(parts[parts.length - 2])} #${Number(last) + 1}`;
  }
  return label(last);
}

export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");
}
