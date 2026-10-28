# Autopost — autonomous data entry

Reads incoming supplier documents, extracts them into a pack-defined schema, validates them
against GST and business rules, and posts the safe ones into TallyPrime. Records that fail a
rule or carry low confidence go to a small review queue with the reason written in plain
language.

This repository implements the build brief and stages 0–1 of the autonomy roadmap in
[the PRD](docs/PRD.md). Manufacturing is the first industry pack; the engine itself knows
nothing about steel pipes.

```
 WhatsApp ─┐                                     ┌─ TallyPrime (bridge agent)
 Email ────┤   FastAPI engine    Supabase        ├─ CSV / Excel export
 Web ──────┼─► classify → extract ─► validate ─► ├─ webhook out
 API ──────┤   (Pydantic AI)  (pack rules)       └─ compliance record
 Bulk ─────┘         │                 │
                     ▼                 ▼
              confidence router ──► review queue (Next.js + WorkOS)
```

## What is built

| PRD | Implemented |
| --- | --- |
| §6.1 Intake | Web upload, pasted text, email/WhatsApp/bulk channels on the same API, sha256 dedupe, multi-document splitting |
| §6.2 Extraction | Pydantic AI agent with strict pack-derived schemas, deterministic text-layer extractor, per-field confidence and source location, retry/escalate on schema failure |
| §6.3 Validation | Declarative rules engine; Appendix B rules as pack JSON (GSTIN format/status, tax type vs place of supply, tax and total arithmetic, HSN rate, duplicates via pgvector, three-way match, GSTR-2B, IRN, e-way bill, TDS, date sanity, bank balance) |
| §6.4 Router and queue | Thresholds per tenant, bands and priority, review queue, record review with field↔document highlighting, approve/edit/reject/hold/request/reassign, bulk approve, exceptions |
| §6.5 / Appendix C | Voucher mapper with GST split and narration reference, idempotency keys, Tally XML + CSV connectors, reversal, master-data proposals |
| §6.6 Learning loop | Corrections stored per tenant, one-off flag, ledger-mapping learning, rule suggestions a supervisor confirms, accuracy by vendor |
| §7 Packs | Versioned manufacturing pack: catalogue, extraction hints, rules, connectors, evaluation set; pack registry and sync |
| §9 Stack | Supabase Postgres (schema per tenant), FastAPI, Pydantic AI, pgvector, Next.js, WorkOS |
| §10 Guardrails | Tenant isolation at schema level plus RLS, no autonomous financial actions, full activity log, review required for government submissions |
| §12a Autonomy | Stage gates as measured checks, per-tenant limits, kill switch, automatic demotion, shadow/draft/autonomous modes |
| Appendix D | The five screens, under-30-second review, plain-language flags, voucher preview, keyboard shortcuts, metrics |
| Appendix E | Bridge agent: outbound-only, signed commands, Tally XML adapter, local queue, heartbeats, master sync, deferral instead of failure, reversal |

### Deliberately not built

Per the PRD's "do not build yet": hosted/server Tally, SAP and e-commerce packs, the public API
and embedded widget, and any pricing or billing beyond counting vouchers, documents and
statement lines. There is no payment or ordering action anywhere in the system.

## Layout

```
engine/                 FastAPI service: intake, extraction, rules, router, posting, API
  api/                  HTTP surface (web app, calling products, bridge agent)
  extraction/           classifier, Pydantic AI agent, deterministic text extractor
  validation/           rules engine, Appendix B checks, GSTN client
  routing/              confidence router, autonomy gates
  posting/              voucher mapper, connectors, signed command queue
  repo/                 every tenant-scoped read and write
packages/packs/         industry packs (manufacturing v1.0.0)
bridge_agent/           Windows agent that talks to TallyPrime on localhost
apps/web/               Next.js app: public landing page (/), then the console at /dashboard,
                        queue, review, bulk, exceptions, activity, settings
supabase/               control-plane migrations and the per-tenant schema
scripts/                migrations, tenant seeding, pack evaluation, smoke run
tests/                  63 tests: rules, router, gates, evaluation, bridge agent
docs/PRD.md             the product requirements this was built from
```

## Design system

The landing page and the console share one design system, ported from the
Stackflo reference design: warm near-white paper (`oklch(0.982 0.006 95)`),
warm near-black ink, Archivo, pill buttons, soft 14px radii and hairline
borders. Every token lives in `apps/web/src/app/globals.css` under `@theme`;
the console's ink scale is inverted for paper, so screens written against
`text-ink-100`/`panel`/`badge` re-tone without touching their markup.

`/` is the marketing page (`src/app/(marketing)/page.tsx`). It is public and
static. The console lives under `src/app/(app)/` behind the WorkOS session.

## Running it

Requirements: Python 3.12, Node 20+, a Supabase project (or any Postgres 15+ with pgvector).

```bash
python -m pip install -r requirements.txt
cp .env.example .env          # fill in DATABASE_URL and ENGINE_SERVICE_TOKEN

python scripts/migrate.py     # control plane + platform policies
python scripts/seed_tenant.py --slug precision-pipes --name "Precision Pipes"

python -m engine.api.server   # http://127.0.0.1:8088
cd apps/web && npm install && npm run dev
```

Next.js reads `apps/web/.env.local` (not the monorepo root). It needs `ENGINE_API_URL`,
`ENGINE_SERVICE_TOKEN`, `ENGINE_DEV_TENANT`, and the WorkOS values below.

### WorkOS

Authentication, organisations, roles and SSO come from WorkOS (§12c). Add to `apps/web/.env.local`:

```
WORKOS_API_KEY=...
WORKOS_CLIENT_ID=...
WORKOS_REDIRECT_URI=http://localhost:3000/callback
WORKOS_COOKIE_PASSWORD=<32+ random characters>
```

Then point the WorkOS redirect URI at `/callback`. Each WorkOS organisation maps to one tenant
through `public.tenants.workos_organization_id`; the organisation in the signed session decides
which Postgres schema a request touches, never anything the browser sends. Roles: `owner`,
`reviewer`, `viewer`.

Until those keys are present the app runs as the seeded demo organisation and says so in a
banner; the engine refuses that path when `ENGINE_ENV=production`.

### Bridge agent

```bash
python -m bridge_agent.cli init --cloud-url https://engine.example --credential-id ... \
    --cloud-public-key ... --tally-url http://127.0.0.1:9000 --tally-company "Precision Pipes"
python -m bridge_agent.cli run       # outbound only; no inbound port
python -m bridge_agent.cli status
```

The agent verifies every command's Ed25519 signature with the cloud's public key, refuses
commands for another tenant or company, checks for an existing voucher carrying the same
idempotency key before posting, defers (rather than fails) when Tally is closed, and writes
results to a local SQLite queue until the cloud acknowledges them.

## Checking it

```bash
python -m pytest                       # 63 tests
python scripts/eval_pack.py            # pack accuracy gate
python scripts/smoke_api.py            # ingest the eval set, work the queue end to end
python scripts/db_inspect.py           # tenants, schemas, packs
```

`scripts/eval_pack.py` measures classification accuracy, per-field accuracy and rule agreement
against the pack's labelled cases and prints each miss.

## Honest gaps

- **The evaluation set is synthetic.** 11 generated cases exercise the pipeline; the stage 1
  gate needs real design-partner documents. The stage 0 gate is intentionally failing on
  "those documents are real" until they exist, and the UI says so.
- **GSTN runs on fixtures.** `GSP_USE_FIXTURE=true` in development. Production must use the
  official GSTN source through an empanelled GST Suvidha Provider (§Appendix B decision).
- **Documents are stored as text pages.** The review pane highlights the recorded location
  line and field; uploading the original file to Supabase Storage and rendering the PDF or
  image with bounding boxes is the next step for scanned documents.
- **No master data has been synced.** Until the bridge agent reads the customer's ledgers,
  vouchers carry proposals ("confirm the party ledger") instead of guessing, which is why
  approving one can send it back with a master-data reason.
- **Open pricing and limits.** No rupee figures are hard-coded; the value cap, daily cap,
  confidence threshold and error-rate limit are settings on the autonomy screen.
