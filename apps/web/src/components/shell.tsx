"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, type ReactElement } from "react";
import {
  IconAlert,
  IconGrid,
  IconInbox,
  IconLayers,
  IconLogout,
  IconPulse,
  IconSliders,
  IconUpload,
} from "./icons";
import { initials } from "@/lib/format";
import type { Session } from "@/lib/session";

export interface QueueCounts {
  pending?: { count: number };
  held?: { count: number };
  clarification_requested?: { count: number };
}

type NavItem = {
  href: string;
  label: string;
  icon: (props: { size?: number }) => ReactElement;
  exact?: boolean;
  badge?: "queue" | "exceptions";
};

const NAV: NavItem[] = [
  { href: "/dashboard", label: "Dashboard", icon: IconGrid, exact: true },
  { href: "/queue", label: "Review queue", icon: IconInbox, badge: "queue" },
  { href: "/bulk", label: "Bulk approve", icon: IconLayers },
  { href: "/exceptions", label: "Exceptions", icon: IconAlert, badge: "exceptions" },
  { href: "/intake", label: "Intake", icon: IconUpload },
  { href: "/activity", label: "Activity", icon: IconPulse },
  { href: "/settings", label: "Autonomy & settings", icon: IconSliders },
];

export function Sidebar({
  counts,
  exceptions,
}: {
  counts: QueueCounts | null;
  exceptions: number | null;
}) {
  const pathname = usePathname();
  const queueCount =
    (counts?.pending?.count ?? 0) +
    (counts?.held?.count ?? 0) +
    (counts?.clarification_requested?.count ?? 0);

  return (
    <aside className="sticky top-0 hidden h-screen w-[236px] shrink-0 flex-col border-r hairline bg-ink-950/60 px-3 py-5 lg:flex">
      <Link href="/dashboard" className="mb-7 flex items-center gap-2.5 px-2">
        <svg width="30" height="30" viewBox="0 0 26 26" fill="none" aria-hidden="true">
          <rect width="26" height="26" rx="8" className="fill-foreground" />
          <path
            d="M8 17.5 13 8l5 9.5"
            stroke="var(--color-background)"
            strokeWidth="2.2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        <span className="leading-tight">
          <span className="block text-[14px] font-semibold tracking-tight text-ink-100">Autopost</span>
          <span className="block text-[10.5px] tracking-wide text-ink-400">data entry engine</span>
        </span>
      </Link>

      <nav className="flex flex-col gap-0.5">
        {NAV.map((item) => {
          const active = item.exact ? pathname === item.href : pathname.startsWith(item.href);
          const badge =
            item.badge === "queue" ? queueCount : item.badge === "exceptions" ? (exceptions ?? 0) : 0;
          const Icon = item.icon;
          return (
            <Link key={item.href} href={item.href} className="nav-item" data-active={active}>
              <Icon size={15} />
              <span className="flex-1">{item.label}</span>
              {badge > 0 ? (
                <span className="mono rounded-full bg-rose-500/15 px-1.5 text-[10.5px] font-semibold text-rose-300">
                  {badge}
                </span>
              ) : null}
            </Link>
          );
        })}
      </nav>

      <div className="mt-auto space-y-2 px-1">
        <div className="rounded-2xl border hairline bg-secondary p-3">
          <div className="label-xs">Pipeline</div>
          <ol className="mt-1.5 space-y-1 text-[11px] text-ink-400">
            <li>Intake → extraction</li>
            <li>Pack rules → router</li>
            <li>Bridge agent → Tally</li>
          </ol>
        </div>
        <p className="px-1 text-[10.5px] leading-relaxed text-ink-600">
          Records data only. Never pays, orders or files.
        </p>
      </div>
    </aside>
  );
}

export function TopBar({
  session,
  mode,
  stage,
  engineUp,
}: {
  session: Session;
  mode: string | null;
  stage: number | null;
  engineUp: boolean;
}) {
  const [open, setOpen] = useState(false);
  const modeTone =
    mode === "autonomous" ? "badge-ok" : mode === "shadow" ? "badge-info" : "badge-warn";

  return (
    <div className="sticky top-0 z-20 flex h-[58px] items-center gap-3 border-b hairline bg-background/85 px-5 backdrop-blur-md">
      <div className="flex min-w-0 items-center gap-2">
        <span className="truncate text-[13px] font-medium text-ink-200">
          {session.organizationName ?? "No organisation"}
        </span>
        <span className="badge badge-muted">tenant</span>
      </div>

      <div className="ml-auto flex items-center gap-2">
        <span className={`badge ${modeTone}`}>
          <span className="live-dot">●</span>
          {mode ? `${mode} mode` : "mode unknown"}
        </span>
        {stage !== null ? <span className="badge badge-muted">stage {stage}</span> : null}
        <span className={`badge ${engineUp ? "badge-ok" : "badge-bad"}`}>
          {engineUp ? "engine online" : "engine offline"}
        </span>

        <div className="relative">
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="flex items-center gap-2 rounded-full border hairline bg-card py-1 pr-2.5 pl-1 hover:bg-muted"
          >
            <span className="grid h-6 w-6 place-items-center rounded-full bg-foreground text-[10.5px] font-semibold text-background">
              {initials(session.name)}
            </span>
            <span className="text-[12px] text-ink-300">{session.role}</span>
          </button>
          {open ? (
            <div className="panel absolute right-0 mt-2 w-60 p-2.5">
              <div className="px-1 pb-2">
                <div className="truncate text-[12.5px] font-medium text-ink-100">{session.name}</div>
                <div className="truncate text-[11px] text-ink-400">{session.email}</div>
              </div>
              <div className="border-t hairline pt-2">
                <div className="flex items-center justify-between px-1 py-1 text-[11px] text-ink-400">
                  <span>Organisation</span>
                  <span className="mono max-w-[110px] truncate">
                    {session.organizationId ?? "demo"}
                  </span>
                </div>
                <a href="/logout" className="nav-item mt-1 text-[12.5px]">
                  <IconLogout size={14} /> Sign out
                </a>
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
