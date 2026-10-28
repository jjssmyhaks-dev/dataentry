"use client";

import Image from "next/image";
import Link from "next/link";
import { useEffect, useState } from "react";

/* ---------------------------------- data ---------------------------------- */

const NAV_LINKS = [
  { label: "Product", href: "#how-it-works" },
  { label: "Compliance", href: "#compliance" },
  { label: "Customers", href: "#stories" },
  { label: "Packs", href: "#packs" },
  { label: "Pricing", href: "#cta" },
];

const PROMPT_EXAMPLES = [
  "Drop the invoice my supplier just sent on WhatsApp",
  "Post it to Tally when the GSTIN and totals check out",
  "Hold anything the three-way match can't clear",
  "Reconcile this bank statement against open bills",
];

const WORDMARKS = [
  { name: "MALHOTRA STEEL", className: "font-bold tracking-[0.25em]" },
  { name: "Vardhaman", className: "font-serif italic text-xl" },
  { name: "Pipeform", className: "font-semibold tracking-tight" },
  { name: "BHAGWATI & CO", className: "font-medium tracking-widest" },
  { name: "optiva", className: "font-bold lowercase tracking-tight" },
  { name: "Rathi Sons", className: "font-serif text-xl" },
  { name: "EXTRUSION", className: "font-extrabold tracking-[0.2em]" },
];

const STEPS = [
  {
    n: "01",
    title: "Receive",
    text: "Invoices arrive on WhatsApp, by email, or as a bulk upload. Every file is hashed and de-duplicated at the door.",
  },
  {
    n: "02",
    title: "Read",
    text: "A vision model reads the page — printed, scanned, photographed or handwritten — and returns the pack's strict schema.",
  },
  {
    n: "03",
    title: "Check",
    text: "The GST rules run in order: GSTIN format and status, tax split against place of supply, per-line arithmetic, duplicates.",
  },
  {
    n: "04",
    title: "Decide",
    text: "Above your threshold with every rule passing, the record posts itself. Everything else goes to a person, with the reason attached.",
  },
  {
    n: "05",
    title: "Post",
    text: "The bridge agent writes the voucher into TallyPrime with the record reference in the narration. Idempotent, and reversible.",
  },
  {
    n: "06",
    title: "Learn",
    text: "Every correction is kept for your tenant only — this vendor's layout, this item's ledger, this rate. Nobody else sees it.",
  },
  {
    n: "07",
    title: "Prove",
    text: "Accuracy is measured per document type and per vendor. Cross the error limit and the tenant drops to draft mode on its own.",
  },
];

const INTEGRATIONS = [
  "TallyPrime",
  "SAP",
  "WhatsApp Business",
  "Gmail",
  "Outlook",
  "Supabase",
  "GSTN",
  "e-invoice IRN",
  "e-way bill",
  "CSV",
  "Excel",
  "Webhook",
];

const GOVERN_POINTS = [
  { title: "Single sign-on", text: "Everyone signs in with Okta, Google or your own SSO, through WorkOS." },
  { title: "One schema per tenant", text: "Each customer's data sits in its own Postgres schema, chosen on the server." },
  { title: "Audit from page to voucher", text: "Every post traces back to the field on the page it was read from." },
  { title: "Autonomy above a threshold", text: "Nothing posts itself unless it passes every rule and every confidence floor." },
  { title: "A kill switch, not a ticket", text: "The owner moves the tenant to draft mode instantly. No deploy, no support call." },
];

const COMPLIANCE = ["SSO", "Role-based access", "Schema isolation", "Full audit log"];

const PACKS = [
  {
    glyph: "◧",
    tone: "bg-chip text-chip-foreground",
    name: "Manufacturing",
    text: "Supplier invoices, purchase orders, challans, e-way bills and bank statements. The launch pack.",
  },
  {
    glyph: "▤",
    tone: "bg-secondary text-secondary-foreground",
    name: "CA firms",
    text: "Client bank statements, vouchers and document intake, priced per statement line.",
  },
  {
    glyph: "◍",
    tone: "bg-foreground text-background",
    name: "Admissions",
    text: "Application forms, mark sheets and fee receipts for schools and colleges.",
  },
  {
    glyph: "◈",
    tone: "bg-secondary text-secondary-foreground",
    name: "Legal & consulting",
    text: "Contracts, case files and client intake for Counsel and similar firms.",
  },
];

const QUOTES = [
  {
    quote:
      "We were paying two people to type invoices into Tally. Now the invoice arrives on WhatsApp and the voucher is already there before the day's payments run.",
    name: "Asha R.",
    role: "Accounts head, steel pipe manufacturer",
    initials: "AR",
  },
  {
    quote:
      "The flag reasons are what sold it. 'GSTIN is cancelled' on the record, not a rule code in a log I have to go and decode.",
    name: "Nikhil M.",
    role: "Partner, chartered accountancy firm",
    initials: "NM",
  },
  {
    quote:
      "It caught a duplicate invoice from a vendor who had changed one digit in the number. That alone paid for the year.",
    name: "Sunita K.",
    role: "Finance controller, plastics manufacturer",
    initials: "SK",
  },
];

const CTA_WORDS = ["supplier invoices", "bank statements", "purchase orders", "e-way bills"];

/* --------------------------------- hooks ---------------------------------- */

function useTypewriter(phrases: string[]) {
  const [text, setText] = useState("");

  useEffect(() => {
    let phrase = 0;
    let char = 0;
    let deleting = false;
    let timer = 0;

    const tick = () => {
      const current = phrases[phrase] ?? "";
      if (!deleting) {
        char += 1;
        setText(current.slice(0, char));
        if (char === current.length) {
          deleting = true;
          timer = window.setTimeout(tick, 1900);
          return;
        }
        timer = window.setTimeout(tick, 42);
      } else {
        char -= 1;
        setText(current.slice(0, char));
        if (char === 0) {
          deleting = false;
          phrase = (phrase + 1) % phrases.length;
          timer = window.setTimeout(tick, 420);
          return;
        }
        timer = window.setTimeout(tick, 22);
      }
    };

    timer = window.setTimeout(tick, 700);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return text;
}

function useRotatingIndex(length: number, intervalMs = 2600) {
  const [i, setI] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => setI((v) => (v + 1) % length), intervalMs);
    return () => window.clearInterval(t);
  }, [length, intervalMs]);
  return i;
}

/* --------------------------------- icons ---------------------------------- */

export function LogoMark({ size = 26 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 26 26" fill="none" aria-hidden="true">
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

function UploadIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true" className="text-muted-foreground">
      <path
        d="M8 10.5V2.5m0 0L4.8 5.7M8 2.5l3.2 3.2M2.5 11v1.5a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1V11"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
      <circle cx="9" cy="9" r="9" className="fill-foreground" />
      <path
        d="m5.5 9.2 2.3 2.3 4.7-4.8"
        stroke="var(--color-background)"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function ArrowRight({ className }: { className?: string }) {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true" className={className}>
      <path
        d="M2.5 7h9m0 0L7.8 3.3M11.5 7l-3.7 3.7"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/* ------------------------------- components ------------------------------- */

function Nav() {
  return (
    <header className="sticky top-0 z-50 border-b border-border/60 bg-background/85 backdrop-blur-md">
      <div className="container-page flex h-16 items-center justify-between">
        <Link href="/" className="flex items-center gap-2.5">
          <LogoMark />
          <span className="text-lg font-semibold tracking-tight">Autopost</span>
          <svg
            width="10"
            height="10"
            viewBox="0 0 10 10"
            fill="none"
            aria-hidden="true"
            className="hidden text-muted-foreground sm:block"
          >
            <path d="m2 3.5 3 3 3-3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
          </svg>
        </Link>

        <nav className="hidden items-center gap-7 md:flex">
          {NAV_LINKS.map((l) => (
            <a
              key={l.label}
              href={l.href}
              className="text-sm text-foreground/80 transition-colors hover:text-foreground"
            >
              {l.label}
            </a>
          ))}
        </nav>

        <div className="flex items-center gap-2.5">
          <Link
            href="/login"
            className="hidden text-sm font-medium text-foreground/80 hover:text-foreground sm:block sm:px-3"
          >
            Log in
          </Link>
          <a href="#cta" className="btn-outline hidden h-10 sm:inline-flex">
            Contact sales
          </a>
          <Link href="/login" className="btn-primary h-10 px-4">
            Get started
          </Link>
        </div>
      </div>
    </header>
  );
}

function Hero() {
  const typed = useTypewriter(PROMPT_EXAMPLES);

  return (
    <section className="relative overflow-hidden pt-14 sm:pt-20">
      <div className="container-page flex flex-col items-center text-center">
        <a href="#how-it-works" className="chip fade-up group hover:bg-muted">
          <span className="rounded-full bg-chip px-2.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-chip-foreground">
            Now onboarding
          </span>
          <span className="font-medium">TallyPrime bridge agent</span>
          <ArrowRight className="text-muted-foreground transition-transform group-hover:translate-x-0.5" />
        </a>

        <h1 className="fade-up mt-8 max-w-4xl text-[2.6rem] font-semibold leading-[1.04] tracking-tight sm:text-6xl md:text-[4.2rem]">
          Your invoice inbox,
          <br className="hidden sm:block" /> posted to Tally. Untouched.
        </h1>

        <p className="fade-up mt-6 max-w-2xl text-base leading-relaxed text-muted-foreground sm:text-lg">
          Autopost reads supplier invoices, purchase orders and bank statements, checks every GST rule, and posts
          the ones it is sure about straight into TallyPrime — so the only records a person sees are the ones that
          genuinely need one.
        </p>

        {/* Prompt box */}
        <div className="fade-up mt-10 w-full max-w-xl rounded-2xl border border-border bg-card p-2 shadow-[0_24px_60px_-24px_rgb(0_0_0/0.18)]">
          <div className="px-3 pt-2 pb-1 text-left">
            <span className="text-base text-muted-foreground sm:text-lg">
              {typed}
              <span className="caret-blink ml-0.5 inline-block h-[1.1em] w-[2px] translate-y-[3px] bg-foreground/70" />
            </span>
          </div>
          <div className="flex items-center justify-between px-1.5 pb-1.5 pt-1">
            <Link
              href="/login"
              className="flex items-center gap-2 rounded-full px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              <UploadIcon />
              Upload documents
            </Link>
            <Link href="/login" className="btn-primary h-10 px-5">
              Start it
            </Link>
          </div>
        </div>
      </div>

      {/* Hero diorama */}
      <div className="relative mx-auto mt-12 max-w-5xl px-6 sm:mt-16">
        <Image
          src="/hero-diorama.jpg"
          alt="A miniature manufacturing plant, with a loading dock, stacked pipe sections and tiny workers moving paperwork"
          width={1600}
          height={1067}
          priority
          className="mx-auto w-full rounded-[2rem] sm:rounded-[2.5rem]"
        />
        <a
          href="#how-it-works"
          className="absolute bottom-8 left-1/2 -translate-x-1/2 rounded-full bg-foreground px-5 py-2.5 text-sm font-medium text-background shadow-lg transition-colors hover:bg-foreground/85"
        >
          Discover how
        </a>
      </div>
    </section>
  );
}

function LogoStrip() {
  const row = [...WORDMARKS, ...WORDMARKS];
  return (
    <section className="border-y border-border/70 py-10">
      <p className="mb-7 text-center text-sm text-muted-foreground">
        Trusted by accounts teams at manufacturers and CA firms
      </p>
      <div className="relative overflow-hidden [mask-image:linear-gradient(to_right,transparent,black_12%,black_88%,transparent)]">
        <div className="animate-marquee flex w-max items-center gap-16 pr-16">
          {row.map((w, i) => (
            <span key={`${w.name}-${i}`} className={`whitespace-nowrap text-lg text-foreground/45 ${w.className}`}>
              {w.name}
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}

function HowItWorks() {
  return (
    <section id="how-it-works" className="py-24 sm:py-32">
      <div className="container-page grid gap-14 md:grid-cols-[1fr_1.3fr]">
        <div className="md:sticky md:top-28 md:self-start">
          <p className="eyebrow">How it works</p>
          <h2 className="mt-4 text-3xl font-semibold tracking-tight sm:text-5xl">
            From a photographed invoice to a Tally voucher.
          </h2>
          <p className="mt-5 max-w-md text-muted-foreground">
            Seven steps, no data-entry team. You forward the document — Autopost brings the rest, including the
            rules that decide whether it may post itself.
          </p>
          <Link href="/login" className="btn-primary mt-8">
            Discover how
          </Link>
        </div>

        <ol>
          {STEPS.map((s) => (
            <li key={s.n} className="flex gap-6 border-t border-border py-7 first:border-t-0 first:pt-0 md:py-8">
              <span className="text-sm font-semibold text-muted-foreground">{s.n}</span>
              <div>
                <h3 className="text-lg font-semibold tracking-tight">{s.title}</h3>
                <p className="mt-1.5 max-w-md text-[15px] leading-relaxed text-muted-foreground">{s.text}</p>
              </div>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}

function ValidationRow({ name, detail }: { name: string; detail: string }) {
  return (
    <div className="flex items-center justify-between rounded-xl border border-white/10 bg-white/5 px-4 py-3">
      <span className="text-sm font-medium">{name}</span>
      <span className="text-xs text-background/60">{detail}</span>
    </div>
  );
}

function Compliance() {
  return (
    <section className="py-6 sm:py-10" id="compliance">
      <div className="container-page">
        <div className="relative overflow-hidden rounded-[2rem] bg-foreground px-6 py-16 text-background sm:rounded-[2.5rem] sm:px-14 sm:py-24">
          <Image
            src="/agents-backdrop.jpg"
            alt=""
            aria-hidden="true"
            width={1600}
            height={912}
            className="pointer-events-none absolute inset-0 h-full w-full object-cover opacity-40"
          />
          <div className="relative">
            <div className="grid items-center gap-12 lg:grid-cols-2">
              <div>
                <p className="text-xs font-semibold uppercase tracking-[0.2em] text-background/60">The gate</p>
                <h2 className="mt-4 text-3xl font-semibold tracking-tight sm:text-5xl">
                  Nothing reaches Tally until the rules agree.
                </h2>
                <p className="mt-5 max-w-lg text-background/70">
                  Compliance is not a report you read at month end. It is the gate every record passes before a
                  voucher exists — and the reason a record is held is written for a person, not for a log.
                </p>
                <div className="mt-8 flex flex-wrap gap-2.5">
                  {["GSTIN", "GSTR-2B", "e-invoice IRN", "e-way bill"].map((n) => (
                    <span
                      key={n}
                      className="rounded-full border border-white/20 bg-white/10 px-4 py-1.5 text-sm font-medium"
                    >
                      {n}
                    </span>
                  ))}
                  <span className="rounded-full bg-background px-4 py-1.5 text-sm font-medium text-foreground">
                    + your own rules
                  </span>
                </div>
              </div>

              <div className="space-y-3">
                <ValidationRow name="GSTIN format" detail="27AABCU9603R1ZM · active" />
                <ValidationRow name="Tax split" detail="intra-state · CGST + SGST" />
                <ValidationRow name="Total arithmetic" detail="differs by ₹12.00" />

                <div className="mt-6 rounded-2xl bg-card p-5 text-foreground shadow-xl">
                  <p className="text-sm font-medium">Why was this record held?</p>
                  <p className="mt-1 text-xs text-muted-foreground">INV-2024-0912 · Precision Pipes</p>
                  <div className="mt-4 overflow-hidden rounded-xl border border-border">
                    <div className="grid grid-cols-3 gap-2 border-b border-border bg-muted px-3 py-2 text-xs font-semibold text-muted-foreground">
                      <span>Rule</span>
                      <span>Result</span>
                      <span>Detail</span>
                    </div>
                    {[
                      ["GSTIN status", "pass", "active since 2019"],
                      ["Total arithmetic", "fail", "12.00 short"],
                      ["Duplicate", "warn", "similar invoice found"],
                    ].map((r) => (
                      <div key={r[0]} className="grid grid-cols-3 gap-2 px-3 py-2 text-xs">
                        <span>{r[0]}</span>
                        <span
                          className={
                            r[1] === "fail"
                              ? "font-medium text-rose-400"
                              : r[1] === "warn"
                                ? "font-medium text-amber-400"
                                : "font-medium text-emerald-400"
                          }
                        >
                          {r[1]}
                        </span>
                        <span className="text-muted-foreground">{r[2]}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

const APP_TILES = ["Review queue", "Bulk approve", "Exceptions", "Intake", "Activity", "Settings"];

function HomeMockups() {
  return (
    <section id="mobile" className="py-24 sm:py-32">
      <div className="container-page">
        <div className="mx-auto max-w-2xl text-center">
          <p className="eyebrow">Operate</p>
          <h2 className="mt-4 text-3xl font-semibold tracking-tight sm:text-5xl">
            One queue. A record cleared in under thirty seconds.
          </h2>
          <p className="mt-5 text-muted-foreground">
            The document on the left, the extracted fields on the right, the voucher underneath. Nobody opens a
            second window to check a number.
          </p>
        </div>

        <div className="mt-14 grid items-center gap-8 lg:grid-cols-[1.6fr_1fr]">
          {/* Desktop mock */}
          <div className="rounded-[1.75rem] border border-border bg-card p-4 shadow-[0_30px_80px_-30px_rgb(0_0_0/0.2)] sm:p-6">
            <div className="flex items-center gap-3 border-b border-border pb-4">
              <LogoMark />
              <div>
                <p className="text-sm font-semibold">Good morning, Asha</p>
                <p className="text-xs text-muted-foreground">23 documents waiting · 4 held for a missing PO</p>
              </div>
              <span className="ml-auto hidden rounded-full border border-border px-3 py-1 text-xs text-muted-foreground sm:block">
                Posting autonomously
              </span>
            </div>
            <p className="mt-5 text-xs font-semibold uppercase tracking-widest text-muted-foreground">Workspace</p>
            <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3">
              {APP_TILES.map((t, i) => (
                <div
                  key={t}
                  className="rounded-xl border border-border bg-background p-4 transition-colors hover:bg-muted"
                >
                  <span
                    className={`inline-flex h-8 w-8 items-center justify-center rounded-lg text-sm font-bold ${
                      i % 3 === 0 ? "bg-foreground text-background" : "bg-secondary text-secondary-foreground"
                    }`}
                  >
                    {t[0]}
                  </span>
                  <p className="mt-2.5 text-sm font-medium">{t}</p>
                  <p className="text-xs text-muted-foreground">
                    {["11 waiting", "4 ready", "2 unreadable", "3 today", "live", "stage 2"][i]}
                  </p>
                </div>
              ))}
            </div>
          </div>

          {/* Phone mock */}
          <div className="mx-auto w-full max-w-[290px] rounded-[2.5rem] border border-border bg-card p-3 shadow-[0_30px_80px_-30px_rgb(0_0_0/0.25)]">
            <div className="rounded-[2rem] bg-background p-5">
              <p className="text-xs text-muted-foreground">9:41</p>
              <p className="mt-3 text-sm font-semibold">Good morning, Diego</p>
              <p className="text-xs text-muted-foreground">4 records need you</p>
              <div className="mt-4 rounded-xl border border-border bg-card px-3 py-2.5 text-xs text-muted-foreground">
                Ask or search a record
              </div>
              <p className="mt-5 text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
                Flagged
              </p>
              <div className="mt-2 space-y-2">
                {[
                  ["INV-0912", "total off by ₹12"],
                  ["PO-0448", "missing challan"],
                  ["INV-0907", "GSTIN cancelled"],
                  ["BS-12", "unmatched line"],
                ].map((r) => (
                  <div key={r[0]} className="flex items-center gap-3 rounded-xl border border-border bg-card px-3 py-2.5">
                    <span className="inline-flex h-7 w-7 items-center justify-center rounded-lg bg-secondary text-xs font-bold text-secondary-foreground">
                      {r[0][0]}
                    </span>
                    <span className="text-xs font-medium">{r[0]}</span>
                    <span className="ml-auto text-[10px] text-muted-foreground">{r[1]}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function Integrations() {
  return (
    <section className="py-24 sm:py-32" id="integrations">
      <div className="container-page grid items-center gap-14 lg:grid-cols-2">
        <div>
          <p className="eyebrow">Integrations</p>
          <h2 className="mt-4 text-3xl font-semibold tracking-tight sm:text-5xl">
            Plugs into the tools you already use
          </h2>
          <p className="mt-5 max-w-lg text-muted-foreground">
            Documents come in from WhatsApp and email, masters come out of TallyPrime, and the GST checks go to
            GSTN. Nothing in your accounting setup gets replaced.
          </p>
          <div className="mt-8 flex flex-wrap gap-2.5">
            {INTEGRATIONS.map((n) => (
              <span key={n} className="chip">
                {n}
              </span>
            ))}
          </div>
          <Link href="/login" className="btn-outline mt-8">
            Explore integrations
            <ArrowRight />
          </Link>
        </div>

        <div className="rounded-[1.75rem] border border-border bg-card p-6 shadow-[0_24px_60px_-28px_rgb(0_0_0/0.18)] sm:p-8">
          <p className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">Try a record</p>
          <p className="mt-4 text-lg font-medium leading-snug">
            Post this invoice to Tally if the GSTIN is active and the totals reconcile.
          </p>
          <div className="mt-6 flex items-center justify-between rounded-xl bg-muted px-4 py-3">
            <span className="text-sm text-muted-foreground">GSTN + TallyPrime connected</span>
            <Link href="/login" className="btn-primary h-9 px-4 text-xs">
              Start it
            </Link>
          </div>
          <div className="mt-5 grid grid-cols-3 gap-3 text-xs text-muted-foreground">
            {["GSTIN · active", "Tax · 18% split", "Total · reconciles"].map((d) => (
              <div
                key={d}
                className="rounded-lg border border-border bg-background px-3 py-2.5 font-medium text-foreground/80"
              >
                {d}
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

function Govern() {
  return (
    <section id="govern" className="py-24 sm:py-32">
      <div className="container-page grid items-start gap-14 lg:grid-cols-2">
        <div>
          <p className="eyebrow">Govern</p>
          <h2 className="mt-4 text-3xl font-semibold tracking-tight sm:text-5xl">
            Your data stays yours, under your control.
          </h2>
          <p className="mt-5 max-w-lg text-muted-foreground">
            Each customer's records sit in their own schema, chosen on the server from the signed session. The
            tenant is never taken from anything a browser sends.
          </p>
          <div className="mt-8 flex flex-wrap gap-2.5">
            {COMPLIANCE.map((c) => (
              <span key={c} className="chip font-semibold">
                {c}
              </span>
            ))}
          </div>
          <Link href="/login" className="btn-outline mt-8">
            Explore enterprise controls
            <ArrowRight />
          </Link>
        </div>

        <ul className="space-y-4">
          {GOVERN_POINTS.map((p) => (
            <li key={p.title} className="flex gap-4 rounded-2xl border border-border bg-card p-5">
              <CheckIcon />
              <div>
                <p className="text-sm font-semibold">{p.title}</p>
                <p className="mt-1 text-sm text-muted-foreground">{p.text}</p>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

function Packs() {
  return (
    <section id="packs" className="py-24 sm:py-32">
      <div className="container-page">
        <div className="mx-auto max-w-2xl text-center">
          <p className="eyebrow">Industry packs</p>
          <h2 className="mt-4 text-3xl font-semibold tracking-tight sm:text-5xl">
            Start from a pack, shape it to your business
          </h2>
          <p className="mt-5 text-muted-foreground">
            A pack is configuration, not code: document schemas, extraction hints, validation rules and the
            evaluation set that gates every release.
          </p>
        </div>

        <div className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {PACKS.map((t) => (
            <div
              key={t.name}
              className="group rounded-2xl border border-border bg-card p-6 transition-shadow hover:shadow-[0_20px_50px_-24px_rgb(0_0_0/0.25)]"
            >
              <span className={`inline-flex h-11 w-11 items-center justify-center rounded-xl text-lg font-bold ${t.tone}`}>
                {t.glyph}
              </span>
              <h3 className="mt-4 font-semibold tracking-tight">{t.name}</h3>
              <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">{t.text}</p>
              <span className="mt-4 inline-flex items-center gap-1.5 text-sm font-medium opacity-0 transition-opacity group-hover:opacity-100">
                Install <ArrowRight />
              </span>
            </div>
          ))}
        </div>

        <div className="mt-10 text-center">
          <Link href="/login" className="btn-primary">
            Explore the packs
          </Link>
        </div>
      </div>
    </section>
  );
}

function Stories() {
  return (
    <section id="stories" className="py-24 sm:py-32">
      <div className="container-page">
        <h2 className="mx-auto max-w-2xl text-center text-3xl font-semibold tracking-tight sm:text-5xl">
          Built for the people who actually run the books
        </h2>
        <div className="mt-12 grid gap-4 md:grid-cols-3">
          {QUOTES.map((q) => (
            <figure key={q.name} className="flex flex-col rounded-2xl border border-border bg-card p-7">
              <blockquote className="text-[15px] leading-relaxed text-foreground/90">“{q.quote}”</blockquote>
              <figcaption className="mt-6 flex items-center gap-3 pt-2">
                <span className="inline-flex h-10 w-10 items-center justify-center rounded-full bg-secondary text-xs font-bold text-secondary-foreground">
                  {q.initials}
                </span>
                <div>
                  <p className="text-sm font-semibold">{q.name}</p>
                  <p className="text-xs text-muted-foreground">{q.role}</p>
                </div>
              </figcaption>
            </figure>
          ))}
        </div>
      </div>
    </section>
  );
}

function FinalCta() {
  const i = useRotatingIndex(CTA_WORDS.length);
  return (
    <section id="cta" className="pb-24 pt-8 sm:pb-32">
      <div className="container-page">
        <div className="rounded-[2rem] bg-foreground px-6 py-20 text-center text-background sm:rounded-[2.5rem] sm:py-28">
          <h2 className="mx-auto max-w-3xl text-3xl font-semibold tracking-tight sm:text-6xl">
            Get your{" "}
            <span
              key={CTA_WORDS[i]}
              className="word-in inline-block text-background/70 underline decoration-background/30 decoration-[3px] underline-offset-8"
            >
              {CTA_WORDS[i]}
            </span>
            <br />
            into Tally without a keystroke.
          </h2>
          <p className="mx-auto mt-6 max-w-xl text-background/70">
            Start with one batch of last month's invoices. Shadow mode shows you the accuracy before anything posts
            by itself.
          </p>
          <div className="mt-9 flex flex-wrap justify-center gap-3">
            <Link href="/login" className="btn-light">
              Start free
            </Link>
            <a href="#cta" className="btn-ghost-light">
              Contact sales
            </a>
          </div>
        </div>
      </div>
    </section>
  );
}

function Footer() {
  const cols: { title: string; links: string[] }[] = [
    { title: "Product", links: ["How it works", "Packs", "Compliance", "Integrations", "Pricing"] },
    { title: "Company", links: ["About", "Customers", "Careers", "Contact"] },
    { title: "Resources", links: ["Docs", "Changelog", "Status", "Help center"] },
    { title: "Legal", links: ["Privacy", "Terms", "Security"] },
  ];
  return (
    <footer className="border-t border-border py-14">
      <div className="container-page">
        <div className="grid gap-10 sm:grid-cols-[1.4fr_repeat(4,1fr)]">
          <div>
            <Link href="/" className="flex items-center gap-2.5">
              <LogoMark />
              <span className="text-lg font-semibold tracking-tight">Autopost</span>
            </Link>
            <p className="mt-4 max-w-xs text-sm text-muted-foreground">
              The autonomous data entry engine that starts with the documents you already receive.
            </p>
          </div>
          {cols.map((c) => (
            <div key={c.title}>
              <p className="text-sm font-semibold">{c.title}</p>
              <ul className="mt-4 space-y-2.5">
                {c.links.map((l) => (
                  <li key={l}>
                    <a
                      href="#"
                      className="text-sm text-muted-foreground transition-colors hover:text-foreground"
                    >
                      {l}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
        <p className="mt-12 text-xs text-muted-foreground">© 2026 Autopost. All rights reserved.</p>
      </div>
    </footer>
  );
}

export function LandingPage() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <Nav />
      <main>
        <Hero />
        <LogoStrip />
        <HowItWorks />
        <Compliance />
        <HomeMockups />
        <Integrations />
        <Govern />
        <Packs />
        <Stories />
        <FinalCta />
      </main>
      <Footer />
    </div>
  );
}
