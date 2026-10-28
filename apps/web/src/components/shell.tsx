"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { IconLogout } from "./icons";
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
  exact?: boolean;
  badge?: "queue" | "exceptions";
};

const NAV: NavItem[] = [
  { href: "/dashboard", label: "Dashboard", exact: true },
  { href: "/queue", label: "Queue", badge: "queue" },
  { href: "/bulk", label: "Bulk approve" },
  { href: "/exceptions", label: "Exceptions", badge: "exceptions" },
  { href: "/intake", label: "Intake" },
  { href: "/activity", label: "Activity" },
  { href: "/settings", label: "Autonomy" },
];

function LogoMark() {
  return (
    <svg width="26" height="26" viewBox="0 0 26 26" fill="none" aria-hidden="true">
      <rect width="26" height="26" rx="8" className="fill-foreground" />
      <path
        d="M8 17.5 13 8l5 9.5"
        stroke="var(--color-background)"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Count pill shared by the desktop and mobile rows. */
function CountPill({ value }: { value: number }) {
  if (value <= 0) return null;
  return (
    <span className="mono inline-flex min-w-[18px] justify-center rounded-full bg-rose-500/12 px-1.5 text-[10.5px] font-semibold text-rose-300">
      {value}
    </span>
  );
}

export function TopNav({
  session,
  mode,
  stage,
  engineUp,
  counts,
  exceptions,
}: {
  session: Session;
  mode: string | null;
  stage: number | null;
  engineUp: boolean;
  counts: QueueCounts | null;
  exceptions: number | null;
}) {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);

  const queueCount =
    (counts?.pending?.count ?? 0) +
    (counts?.held?.count ?? 0) +
    (counts?.clarification_requested?.count ?? 0);
  const modeTone =
    mode === "autonomous" ? "badge-ok" : mode === "shadow" ? "badge-info" : "badge-warn";

  const badgeFor = (item: NavItem) =>
    item.badge === "queue" ? queueCount : item.badge === "exceptions" ? (exceptions ?? 0) : 0;
  const isActive = (item: NavItem) =>
    item.exact ? pathname === item.href : pathname.startsWith(item.href);

  return (
    <header className="sticky top-0 z-40 border-b border-border/70 bg-background/85 backdrop-blur-md">
      <div className="container-app flex h-16 items-center gap-4">
        <Link href="/dashboard" className="flex shrink-0 items-center gap-2.5">
          <LogoMark />
          <span className="text-[17px] font-semibold tracking-tight">Autopost</span>
        </Link>

        <nav className="hidden items-center gap-1 lg:flex">
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[13.5px] transition-colors ${
                isActive(item)
                  ? "bg-foreground text-background"
                  : "text-foreground/75 hover:bg-muted hover:text-foreground"
              }`}
            >
              {item.label}
              <CountPill value={badgeFor(item)} />
            </Link>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-2">
          <span className={`badge ${modeTone} hidden sm:inline-flex`}>
            <span className="live-dot">●</span>
            {mode ? `${mode} mode` : "mode unknown"}
          </span>
          {stage !== null ? <span className="badge badge-muted hidden md:inline-flex">stage {stage}</span> : null}
          <span className={`badge ${engineUp ? "badge-ok" : "badge-bad"} hidden sm:inline-flex`}>
            {engineUp ? "engine online" : "engine offline"}
          </span>

          <div className="relative">
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              className="flex items-center gap-2 rounded-full border border-border bg-card py-1 pr-3 pl-1 transition-colors hover:bg-muted"
              aria-label="Account menu"
            >
              <span className="grid h-7 w-7 place-items-center rounded-full bg-foreground text-[11px] font-semibold text-background">
                {initials(session.name)}
              </span>
              <span className="hidden text-[12px] text-ink-300 sm:block">{session.role}</span>
            </button>
            {open ? (
              <div className="panel absolute right-0 mt-2 w-64 p-3">
                <p className="text-[10px] font-semibold tracking-[0.16em] text-muted-foreground uppercase">
                  Signed in
                </p>
                <p className="mt-1.5 truncate text-[13px] font-medium text-ink-100">{session.name}</p>
                <p className="truncate text-[11.5px] text-ink-400">{session.email}</p>
                <div className="mt-3 space-y-1.5 border-t border-border pt-3 text-[11.5px] text-ink-400">
                  <div className="flex items-center justify-between gap-2">
                    <span>Organisation</span>
                    <span className="truncate font-medium text-ink-200">
                      {session.organizationName ?? "none"}
                    </span>
                  </div>
                  <div className="flex items-center justify-between gap-2">
                    <span>Tenant</span>
                    <span className="mono truncate text-[11px] text-ink-300">
                      {session.tenantSlug ?? session.organizationId ?? "demo"}
                    </span>
                  </div>
                </div>
                <a href="/logout" className="nav-item mt-2 text-[12.5px]">
                  <IconLogout size={14} /> Sign out
                </a>
              </div>
            ) : null}
          </div>
        </div>
      </div>

      {/* Small screens: the same links, scrollable under the header */}
      <div className="container-app flex gap-1 overflow-x-auto pb-2 lg:hidden">
        {NAV.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className={`flex shrink-0 items-center gap-1.5 rounded-full px-3 py-1.5 text-[12.5px] transition-colors ${
              isActive(item)
                ? "bg-foreground text-background"
                : "border border-border bg-card text-foreground/80"
            }`}
          >
            {item.label}
            <CountPill value={badgeFor(item)} />
          </Link>
        ))}
      </div>
    </header>
  );
}
