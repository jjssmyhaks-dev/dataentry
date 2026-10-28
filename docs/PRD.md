# PRD: Autonomous Data Entry Platform

## 1. Overview

A standalone, industry-agnostic AI product that autonomously performs data entry: it reads incoming documents, extracts structured data, validates it against business and government rules, and posts it into the customer's system of record. High-confidence records go through untouched; uncertain ones go to a small human review queue.

The product launches with **manufacturing** (including the government/compliance side) and is designed so that new industries are added as **Industry Packs** without rebuilding the engine. It also serves as a shared backbone for Ygudy's other products (Counsel, the CA firm product, the admissions SaaS for schools/colleges/institutes, and the AI Teammates suite for manufacturers).

## Build brief for the coding agent

This section sets out what is decided and where to start, so building can begin without waiting on further answers. Where it conflicts with an older line elsewhere in this document, this brief and the sections it names win.

**Decisions that are final**

- Product: a standalone autonomous data entry platform, manufacturing first, with industry packs as configuration (sections 1 and 7).
- Tally: latest TallyPrime only, not Tally ERP 9. Tally on the customer's own PC only for version one; hosted Tally comes later (Appendix E).
- Posting: the bridge agent posts autonomously from launch, with draft mode as the fallback and per-tenant option (section 6.5a and Appendix C). Autonomy is released in stages with gates (section 12a).
- Compliance data: the official GSTN source through an empanelled GST Suvidha Provider; third-party verification APIs for prototypes only (Appendix B).
- Pricing: voucher-based hybrid, with a separate bank statement price (section 12b). No rupee figures are set; do not hard-code prices.
- Stack and authentication: Supabase Postgres with a schema per tenant, FastAPI on Cloud Run, Pydantic AI, Inngest, Langfuse, Next.js and WorkOS (section 12c).

**Suggested build order**

1. Foundations: tenant model with WorkOS organisations mapped to Postgres schemas, roles, activity log and the Next.js shell.
2. Intake and extraction for supplier invoices, with the schema in Appendix A.
3. The validation rules engine and the GST rules in Appendix B, loaded from pack configuration.
4. The confidence router and the review queue in Appendix D.
5. The bridge agent in read-only mode first: master data sync and a test post with reversal on a test company (Appendix E).
6. Shadow mode, then the autonomy stages and gates in section 12a.

**Do not build yet**

- Hosted or server Tally, SAP, e-commerce packs, the public API and the embedded widget (later phases in section 12).
- Any pricing or billing logic beyond counting vouchers, documents and statement lines per tenant.

**Still open, and safe to leave configurable**

- The acceptable error rate for auto-posted vouchers; the gate numbers in section 12a are proposals to be tuned with a design partner, so keep them as settings.
- Maintenance fee sizing, reseller pricing for CA firms, always-on device needs, and reviewer access limits per client. Each has an open question in its own section.

## 2. Problem

Indian SMEs and professional firms spend large amounts of staff time typing data from invoices, purchase orders, challans, bank statements, forms and WhatsApp photos into Tally, SAP, ERPs, student records, or government portals. This is slow, error-prone, and expensive, and errors surface later as GST mismatches, reconciliation gaps, and compliance penalties.

## 3. Goals and Non-Goals

**Goals**

- Autonomously process the majority of documents with no human touch in the first target workflow (supplier invoice to Tally).
- Make compliance validation (GSTIN, GST maths, e-invoice, e-way bill) a first-class feature, not an afterthought.
- Make adding a new industry a matter of weeks of pack authoring, not engineering rewrites.
- Be embeddable inside every other Ygudy product via API and widget.

**Non-Goals (v1)**

- Hardware, robotics, or RPA screen-scraping as the primary integration method.
- Autonomous financial actions such as paying suppliers or placing orders. The product records data; it does not move money.
- Filing returns directly with government portals without human approval.

## 4. Target Users

- **Primary (v1):** Indian manufacturers (e.g. steel pipes, plastics) with accounts and purchase staff entering data into TallyPrime or SAP.
- **Secondary:** CA firms handling client bookkeeping and bank statements; admissions teams at schools, colleges and universities; law and consulting firms (Counsel) for intake and case documents.
- **Internal:** Other Ygudy products consuming the engine through API.

## 5. Core Concepts

- **Engine:** The generic pipeline of intake, extraction, validation, routing, posting, and learning.
- **Industry Pack:** A versioned bundle that teaches the engine one industry. Contains a document catalogue, extraction hints, validation rules, connectors, and an evaluation set.
- **Tenant:** One customer organisation with isolated data, its own pack configuration, and its own learned corrections.
- **Confidence Router:** The decision step that auto-posts high-confidence records and sends the rest to review.

## 6. Functional Requirements

### 6.1 Intake

- Channels: WhatsApp Business API, email inbox per tenant, web upload, bulk upload, and API/webhook.
- Accept PDFs, images, scanned documents, spreadsheets, and bank statement exports.
- Classify each document by type using the tenant's active packs.
- Deduplicate at intake using file hash and content similarity.

### 6.2 Extraction

- Vision-capable model reads each document and returns data conforming to the pack's strict schema (Pydantic models).
- Output that fails schema validation is retried or escalated, never silently accepted.
- Per-field confidence scores are captured.
- Handles multi-page documents, multiple documents per file, handwritten and photographed documents, and mixed languages including Indian languages.

### 6.3 Validation

Driven by pack configuration through a rules engine, returning pass, warn, or fail per rule.

- Arithmetic checks: line totals, tax splits, and grand totals.
- GSTIN format and active-status verification through GSTN APIs.
- Three-way match: invoice against purchase order against goods receipt.
- Duplicate detection using pgvector similarity plus key-field matching.
- Government rules (manufacturing pack): GSTR-2B matching, e-invoice IRN checks, e-way bill consistency, and TDS details.
- Bank statement entries: categorisation and ledger matching.

### 6.4 Confidence Router and Review Queue

- Auto-post when overall confidence and all rule results meet the tenant's thresholds.
- Otherwise route to a review queue showing the document, extracted fields, and exactly which fields or rules triggered review.
- Reviewer actions: approve, correct, reject, or request clarification.
- Thresholds configurable per tenant and per document type, starting conservative.

### 6.5 Posting and Connectors

- Connector interface is common across all destinations: validate payload, post, confirm, and roll back.
- v1 connectors: TallyPrime, SAP, CSV/Excel export, and webhook out.
- Planned connectors: government portal payload preparation, other ERPs, and student information systems.
- Every post is idempotent and logged with an audit trail linking back to the source document.

### 6.5a TallyPrime Connector

TallyPrime usually runs as a desktop app on the customer's own machine, so the connector ships in two modes: a local bridge agent that posts autonomously, and file import as the fallback. Autonomous posting is the launch strategy; draft mode with human approval is the fallback if autonomous posting fails to earn customer trust or accuracy targets.

**Mode A: Local bridge agent (primary)**

- A small installable agent on the customer's PC talks to TallyPrime through its built-in XML interface on a local port.
- The agent connects outward to the cloud over a secure, authenticated channel, so no inbound firewall changes are needed.
- It launches in **autonomous mode: records that pass validation and clear the confidence threshold are posted to Tally directly, and only flagged records go to the review queue. Draft mode, where vouchers are staged in Tally for approval before posting, stays built in as a per-tenant setting and as the fallback strategy if autonomous posting does not meet accuracy or trust targets**.
- It reads back ledgers, vendors, stock items and GST settings so extraction and validation match the customer's existing masters.

**Mode B: File import (fallback)**

- The platform generates a Tally-compatible XML or Excel import file for the user to import manually.
- Used for customers who cannot install the agent, and as a safe path while the agent is being rolled out.

**Connector responsibilities (both modes)**

- Map vendors, items and expense heads to the customer's existing ledgers, and propose new ledgers for review rather than creating them silently.
- Build the voucher with the correct GST split (CGST, SGST, IGST) and place-of-supply logic.
- Post idempotently, so retries never create duplicate vouchers.
- Write the Tally voucher number and status back to the audit trail against the source document.

**Agent requirements**

- Windows installer with silent auto-update and a health check that shows connection status in the web app.
- Support for multiple Tally companies and financial years per tenant.
- Clear failure handling: if Tally is closed or the company is not loaded, jobs queue and retry instead of failing.

**Open questions**

- Which TallyPrime versions to support at launch.
- Whether to offer a hosted Tally option for customers who want fully cloud posting.
- How much of the agent is built before the first paying customer, versus launching on Mode B first.

### 6.6 Learning Loop

- Every human correction is stored per tenant, for example vendor-specific layouts and ledger mappings.
- Corrections feed few-shot examples and rule suggestions for that tenant only.
- Accuracy dashboards by tenant, pack, document type, and field.

## 7. Industry Pack System

Each pack is a versioned, self-contained bundle with five parts.

1. **Document catalogue:** every document type with its schema, mandatory fields, and types.
2. **Extraction hints:** industry-specific instructions and examples, for example how steel pipe invoices express weight versus quantity.
3. **Validation rules:** declarative configuration read by the rules engine, not hard-coded.
4. **Connectors:** adapters mapping validated records to target systems.
5. **Evaluation set:** real, labelled documents used to measure accuracy and gate every release.

A **pack registry** loads packs per tenant. Packs are versioned and can be rolled out to one tenant first, with rollback. Tenants can layer a custom pack over a standard one for their own forms.

**Planned packs**

- Manufacturing and compliance (launch).
- CA firm: bank statements, vouchers, client documents.
- Admissions: application forms, mark sheets, fee receipts.
- Legal and consulting: contracts, case files, client intake.
- E-commerce: marketplace settlement reports, return slips, courier manifests.

## 8. Integration Layer

The engine is exposed to the rest of the Ygudy product family in two ways.

- **API and webhooks:** a calling product submits a document and pack, and receives a validated structured record, status events, and review links.
- **Embedded widget or shared login:** users inside Counsel, the CA product, the admissions SaaS, or AI Teammates use data entry features without leaving their product.

**Tenancy mapping:** a customer in any calling product maps to one tenant in this product, with an isolated schema, so data never crosses products or customers.

**Integration principles**

- Calling products own the user relationship; this product owns extraction, validation, and posting.
- Each calling product ships as its own pack plus connectors, not as custom engine code.
- Authentication through service credentials per calling product plus tenant scoping.

## 9. Technical Architecture

Built on the consistent serverless stack used across Ygudy's agentic products.

- **Agents:** Pydantic AI for extraction and classification agents with strict typed outputs.
- **API:** FastAPI on Cloud Run.
- **Data:** Supabase Postgres with schema-per-tenant multi-tenancy; pgvector for duplicate detection and example retrieval.
- **Workflows:** Inngest for durable steps, retries, approvals, and scheduled reconciliation.
- **Observability:** Langfuse for traces, cost, and accuracy tracking.
- **Web app:** Next.js for the review queue and admin; WorkOS for authentication.
- **Integrations:** WhatsApp Business API, TallyPrime, SAP, GSTN APIs.
- **New component:** a declarative rules engine that loads validation rules from pack configuration.

## 10. Safety and Guardrails

- No autonomous financial actions: the product records data, it never pays or orders.
- Low-confidence or rule-failing records never auto-post.
- Government submissions require human approval in v1.
- Full audit trail from every posted entry back to its source document and every edit.
- Tenant isolation enforced at the schema level.
- Personal and financial data handled under Indian data protection requirements, with retention controls per tenant.

## Appendix A: Manufacturing Pack Document Schemas

The manufacturing pack ships with six document types. Each has a strict schema, and fields marked mandatory must be present and valid before a record can auto-post.

| Document type | Key fields | Mandatory fields | Posts to |
| --- | --- | --- | --- |
| Supplier invoice | Vendor name, vendor GSTIN, invoice number, invoice date, line items (description, HSN, quantity, unit, rate, amount), taxable value, CGST, SGST, IGST, round-off, total, place of supply | Vendor GSTIN, invoice number, invoice date, line items, tax split, total | Purchase voucher |
| Purchase order | PO number, PO date, vendor, line items, agreed rate, delivery date, payment terms | PO number, vendor, line items, rate | PO record for matching |
| Goods receipt / delivery challan | Challan number, date, vendor, PO reference, items received, quantity received, vehicle number | Challan number, items received, quantity received | Receipt record for matching |
| E-way bill | E-way bill number, generation date, supplier and recipient GSTIN, document number, HSN, value, vehicle number, validity | E-way bill number, both GSTINs, document number, value | Compliance record |
| E-invoice | IRN, acknowledgement number and date, QR code data, supplier and buyer GSTIN, document number, value | IRN, acknowledgement number, both GSTINs, value | Compliance record, linked to invoice |
| Bank statement | Account reference, date, narration, debit, credit, balance, reference number | Date, amount, debit or credit flag, narration | Bank and ledger entries |

**Schema rules shared by all types**

- Every field carries a value, a confidence score, and a pointer to where it was found on the page.
- Amounts are stored in rupees with two decimals; dates are normalised to one format; GSTINs are stored uppercase and trimmed.
- Line items keep their original order and page reference.
- A document that contains several documents, such as a bundle of invoices in one PDF, is split before extraction.

## Appendix B: GST and Compliance Validation Rules

These rules are declarative configuration in the manufacturing pack. Each returns pass, warn or fail. Any fail blocks auto-post; a warn lowers confidence and may route the record to review depending on tenant thresholds.

| Rule | What it checks | Outcome if it breaks |
| --- | --- | --- |
| GSTIN format | 15 characters, valid state code, PAN pattern, and checksum character | Fail |
| GSTIN active status | Vendor GSTIN is active and not cancelled or suspended, via GSTN APIs | Fail if cancelled, warn if the lookup is unavailable |
| Tax type vs place of supply | CGST and SGST used for intra-state, IGST for inter-state, based on vendor state and place of supply | Fail |
| Tax arithmetic | Taxable value times rate equals tax amount per line, within a rounding tolerance | Fail beyond tolerance, warn within it |
| Total arithmetic | Line totals plus taxes plus round-off equal the invoice total | Fail |
| HSN and rate check | HSN code is valid and the GST rate matches the expected rate for that HSN | Warn |
| Duplicate invoice | Same vendor and invoice number, or near-identical content found by similarity search | Fail on exact match, warn on near match |
| Three-way match | Invoice quantity and rate agree with the purchase order and goods receipt within tolerance | Warn on small variance, fail on large variance |
| GSTR-2B match | Invoice appears in the vendor's filed return with matching value and tax | Warn if missing or mismatched, so input credit risk is visible |
| E-invoice IRN | Where e-invoicing applies, the IRN is present and consistent with the invoice details | Fail if required and missing |
| E-way bill consistency | Where applicable, e-way bill value, GSTINs and document number match the invoice | Warn |
| TDS applicability | Vendor category and amount suggest TDS, and the section and rate are captured | Warn |
| Date sanity | Invoice date is not in the future and falls within an open financial period | Fail |

**Rule engine requirements**

- Rules are versioned with the pack, so a change in GST rules ships as a pack update, not an engine release.
- Thresholds and tolerances are configurable per tenant.
- Every rule result is stored with the record, so reviewers see exactly why a document was flagged.
- Rules that depend on external lookups, such as GSTN, degrade to a warning when the service is down, and the record is rechecked later.

**Decision: GSTN data source**

GSTIN status and taxpayer checks will use the official GSTN source run by the Government of India, through its developer portal and an empanelled GST Suvidha Provider. Third-party verification APIs may be used for prototypes only and are not the production source.

To confirm: the onboarding steps, credentials, cost and rate limits of the chosen provider, and which checks (GSTIN status, GSTR-2B matching, e-invoice and e-way bill lookups) each access level allows.

**Open questions**

- Which GSTN data source to use for active-status and GSTR-2B checks, and its cost and rate limits.
- Turnover thresholds that decide when e-invoicing applies to each tenant.
- How aggressive the default tolerances should be before real data is available.

## Appendix C: Tally Voucher Mapping

Every approved record becomes one TallyPrime voucher. In draft mode, the voucher is staged and marked for human approval before it is posted. This table maps each document type to its voucher and the entries it creates.

| Document type | Tally voucher | Ledger entries created | Notes |
| --- | --- | --- | --- |
| Supplier invoice (goods) | Purchase | Debit purchase ledger or stock item per line; debit input CGST, SGST or IGST; credit supplier party ledger; round-off ledger if any | Line items map to stock items when inventory is on, else to the purchase ledger |
| Supplier invoice (services or expenses) | Purchase or Journal | Debit expense ledger; debit input GST; credit supplier; TDS payable credit where applicable | Expense ledger comes from vendor history or the pack's category rules |
| Supplier credit note | Debit Note | Reverse of the original purchase entries | Linked to the original invoice number |
| Purchase order | Purchase Order | Order lines with rate and delivery date | Created only if the tenant uses order processing in Tally |
| Goods receipt or challan | Receipt Note | Stock items and quantities received | Created only if the tenant uses order processing and receipt notes |
| Bank statement line (payment out) | Payment | Debit party or expense ledger; credit bank ledger | Matched to an open supplier bill where possible |
| Bank statement line (receipt in) | Receipt | Debit bank ledger; credit party or income ledger | Matched to an open customer bill where possible |
| Bank charges and interest | Payment or Receipt | Bank charges or interest ledger against the bank ledger | Rule-based; low risk, so a good candidate for earlier auto-post |
| E-way bill and e-invoice | No voucher | Stored as a compliance record linked to the voucher | Reference number is written into the voucher narration |

**Master data handling**

- Parties: the connector matches the supplier by GSTIN first, then by name. An unmatched supplier is created as a new party ledger only after human approval.
- Ledgers and stock items: the connector reads the tenant's existing masters from Tally and maps to them. It never creates or renames a ledger without approval.
- Tax ledgers: input CGST, SGST and IGST ledgers are mapped once during onboarding, per tenant.
- Cost centres and godowns: mapped only when the tenant uses them.

**Autonomous mode behaviour (default at launch)**

- A record posts directly when every validation rule passes and every field is above its confidence threshold; warnings or failures route to the review queue instead.
- Every posted voucher carries the engine's record reference in its narration, and every post is logged so it can be traced and reversed.
- Reversal: a posted voucher can be undone from the engine by posting a reversing entry or deleting the voucher, within an open financial period.
- Safety limits per tenant: a maximum value for auto-post, a daily count cap, and a kill switch that moves the tenant to draft mode instantly.
- Accuracy is tracked per document type and per vendor; if the error rate crosses a set limit, the tenant is moved to draft mode automatically and the team is alerted.
- Autonomy comes with a paid maintenance and monitoring service: ongoing accuracy review, rule tuning, master data upkeep and pack updates.

**Draft mode behaviour (fallback and per-tenant option)**

- Each voucher carries the engine's record reference in its narration, so any voucher can be traced back to the source document.
- Staged vouchers appear in Tally as unposted or optional vouchers, depending on the version, and the engine shows the same record as awaiting approval.
- Approval in the review queue releases the voucher for posting; rejection removes the staged voucher.
- Edits made in Tally after staging are detected by the agent and flagged back to the engine, not overwritten.

**Error handling**

- A post that Tally rejects, for example a missing ledger or a locked period, returns the Tally error text to the review queue as a plain-language reason.
- Repeated posting is prevented by an idempotency key made from the tenant, document and voucher type.
- If the bridge agent is offline, vouchers wait in a queue and send when it reconnects.

**Open questions**

- Whether optional vouchers or a custom voucher type is the better draft container across TallyPrime versions.
- How to handle multi-currency invoices and import purchases in v1.
- Whether to support Tally's batch-wise and expiry-tracked stock items at launch.

## Appendix D: Review Queue Design

The review queue is where a person confirms or fixes records the engine was not confident about, and where draft-mode vouchers get approved. The design goal is that a reviewer clears a typical record in under 30 seconds without opening the original document in another window.

**Screens**

| Screen | Purpose | Main contents |
| --- | --- | --- |
| Queue list | See what needs attention and pick the next item | Records sorted by priority; filters for document type, vendor, reason flagged, age and assignee; counts per status |
| Record review | Verify and correct one record | Source document on the left with highlighted field locations; extracted fields on the right with confidence and rule results; Tally voucher preview below |
| Bulk approve | Clear many low-risk records at once | Group by vendor or reason; shows count, total value and any warnings before confirming |
| Exceptions | Handle records the engine could not process | Unreadable files, unsupported formats, split failures, with a retry or reject action |
| Activity and audit | Trace what happened to any record | Who changed what and when, rule results, and the Tally post result |

**Record review layout and behaviour**

- Clicking a field on the right highlights its location on the source document, and clicking a spot on the document fills the selected field.
- Fields below the confidence threshold or failing a rule are marked and listed first; passing fields are collapsed.
- Every flag shows a plain-language reason, for example: GSTIN is cancelled, or invoice total differs from line sum by 12 rupees.
- The voucher preview shows ledgers, amounts and GST split exactly as they would appear in Tally.
- Keyboard shortcuts for approve, reject, next item and jump to next flagged field.

**Actions**

- **Approve:** releases the voucher for posting to Tally.
- **Edit and approve:** saves corrections, then releases. Each correction is stored for the learning loop.
- **Reject:** removes the record with a mandatory reason, such as duplicate, not a business document, or wrong tenant.
- **Hold:** parks a record with a note, for example waiting for a missing purchase order.
- **Request document:** sends a templated message to the sender over WhatsApp or email asking for a clearer copy or a missing page.
- **Reassign:** moves a record to another reviewer or to a supervisor.

**Priority and routing**

- Default order is rule failures first, then low confidence, then oldest first, with high-value records ahead of low-value ones.
- Records near a due date, such as GST return cut-offs or vendor payment dates, are raised in priority.
- Approval rights can be limited by amount, so a record above a tenant-set value needs a supervisor.

**Learning from reviewers**

- Corrections feed the per-tenant learning loop, for example vendor-to-ledger mappings and field position hints.
- A reviewer can mark a correction as a one-off so it does not become a learned rule.
- Repeated identical corrections prompt a suggestion to turn them into a tenant rule, which a supervisor must confirm.

**Metrics the queue reports**

- Median review time per record, and share of records approved without edits.
- Backlog size and age, per document type.
- Most common flag reasons, so pack rules and extraction can be improved.

**Open questions**

- Whether the first release is web-only or also includes a mobile view for owners approving on the go.
- Whether reviewers are the customer's staff, Ygudy's operations team, or both, and how access is split.
- What the default value limit for single-reviewer approval should be.

## Appendix E: Bridge Agent Architecture

The bridge agent is a small program on the customer's PC that connects the cloud engine to TallyPrime. It makes only outbound connections, it never holds the customer's documents longer than needed, and it can be paused from the cloud at any time.

&#91;embedded content: bridge agent connections · 3 parts, outbound only\]

No inbound port is opened at the customer site: the agent dials out, and the cloud's commands travel back on that same connection.

**Components**

| Component | Responsibility |
| --- | --- |
| Cloud command service | Holds the queue of vouchers to post and master data requests per tenant; signs each command and records its result |
| Agent connection | A persistent outbound secure connection from the agent to the cloud; receives commands and sends back results and heartbeats |
| Tally adapter | Talks to TallyPrime through its local XML interface on localhost; builds voucher XML, posts it, reads masters, and parses Tally's replies |
| Local queue | A small on-disk store of commands and results so nothing is lost if the connection or Tally drops |
| Master data sync | Reads ledgers, parties, stock items, tax settings and open periods from Tally on a schedule and on demand, and sends them to the cloud |
| Agent supervisor | Starts the agent with the PC, restarts it on failure, checks Tally is running with the right company open, and handles updates |
| Local status screen | A simple tray icon and page showing connected, last post, queue size and any error in plain language |

**Post flow**

1. The engine decides a record is ready under the tenant's autonomy stage and creates a post command with an idempotency key.
2. The cloud signs the command and sends it to the agent over the open connection.
3. The agent checks the signature, the company, and that the period is open, then builds the voucher XML.
4. The Tally adapter posts it and reads Tally's reply, including the voucher identifier.
5. The agent sends the result back; the engine marks the record posted, or routes it to the review queue with Tally's error in plain words.
6. If the connection is down, the command waits in the cloud queue and the result waits in the local queue until it returns.

**Security**

- Outbound-only connection, so no firewall or router changes at the customer site.
- Each agent has its own identity and is bound to one tenant and one Tally company; commands for anything else are refused.
- Commands are signed by the cloud and checked by the agent, so a stolen connection cannot post vouchers.
- Document images are not sent to the agent; it receives only structured voucher data.
- Local logs hold no full document content, and the agent can be revoked from the cloud immediately.
- Installer is signed and updates are signed, with rollback if a new version fails its startup check.

**Reliability**

- Idempotency keys prevent a retry from creating a duplicate voucher; the agent checks for an existing voucher with the key in its narration before posting.
- Heartbeats let the cloud show each customer as connected or offline, and alert the team if an agent stays offline past a set time.
- Commands expire if not delivered in time, and an expired command returns the record to the review queue rather than posting late.
- Posting is paced so Tally is never overloaded, and the agent pauses if Tally is busy or showing a dialog.
- Reversal is a command like any other, so an error can be undone from the engine within an open period.

**Constraints to design around**

- TallyPrime's XML interface must be enabled by the customer, and its behaviour differs across versions.
- Tally allows one company context at a time in many setups, so the agent must confirm the right company is open before every post.
- Users working inside Tally while the agent posts can cause conflicts, so the agent avoids posting during detected activity where possible.
- The PC may be shut off after office hours, so the queue must tolerate long gaps.

**Rollout of the agent**

- Windows is the first and only target; installer for a single PC, installed by our team during onboarding.
- Phase one is read-only, syncing masters and testing a voucher post and reversal on a test company.
- Phase two enables posting for shadow mode comparisons, then the autonomy stages from section 12a.
- Remote install by the customer comes later, once the installer has been proven in the field.

**Decisions made**

- Launch supports the latest TallyPrime only. Older Tally ERP 9 is not supported, which keeps agent testing to one interface generation.
- Version one supports Tally on the customer's own PC only. Server or hosted Tally comes in a later release.
- Still open: whether customers with no always-on PC need a small always-on device, or wait for the hosted option.

## 11. Success Metrics

- Straight-through processing rate: share of documents auto-posted with no human touch.
- Field-level extraction accuracy on the pack evaluation set.
- Error rate on auto-posted records (target near zero, tracked via later corrections).
- Average time from document received to posted.
- Reviewer time per flagged document.
- Time to launch a new industry pack.
- Hours of manual entry saved per tenant per month.

## 12. Roadmap

1. **Phase 1:** Supplier invoice to TallyPrime for manufacturers, with GSTIN and arithmetic validation, review queue, and WhatsApp and email intake.
2. **Phase 2:** Purchase order and goods receipt matching, bank statement processing, and GSTR-2B reconciliation.
3. **Phase 3:** E-invoice and e-way bill preparation, SAP connector, and the learning loop with per-tenant dashboards.
4. **Phase 4:** Public API and embedded widget; integrate with the CA firm product, Counsel, and the admissions SaaS through their own packs.
5. **Phase 5:** E-commerce and other industry packs, and a pack authoring toolkit.

## 12a. Autonomy Roadmap and Gates

Autonomous posting to Tally is the launch strategy, so the roadmap is built around earning it safely. The engine runs in shadow mode first, then posts on its own in widening steps, and each step opens only when its gate is passed. The thresholds below are starting targets to be tuned with real customer data.

| Stage | What the engine does | Gate to enter the next stage |
| --- | --- | --- |
| 0. Foundations | Builds intake, extraction, validation rules, the review queue and the Tally bridge agent. Posts nothing. | Manufacturing pack schemas and GST rules (Appendices A and B) implemented; evaluation set of real documents assembled; bridge agent can stage and post a test voucher and reverse it. |
| 1. Shadow mode | Processes real documents for design-partner customers and records what it would have posted. Humans still do the entry. | Field-level accuracy on the evaluation set meets target for the top document types; every discrepancy against the human entry is reviewed; no unexplained wrong-ledger cases. |
| 2. Autonomous on low-risk | Posts on its own for the safest records only: repeat vendors with a learned ledger mapping, passing every rule, below a low value cap. Everything else goes to the review queue. | Error rate on auto-posted vouchers stays under the agreed limit over a fixed volume and period; zero unreversed errors; customer confirms reconciliation matches. |
| 3. Autonomous on standard | Raises the value cap and widens to all supplier invoices passing validation, then adds bank statement lines and purchase order matching. | Same accuracy limit holds at higher volume; review queue share and median review time are falling; no repeat of any earlier error type. |
| 4. Full autonomy with monitoring | Posts everything that clears rules and confidence, including compliance-linked documents. The maintenance service monitors accuracy, tunes rules and updates packs. | Ongoing: error rate per document type and vendor stays inside limits; any breach triggers the fallback below. |

**Fallback to draft mode**

- Each tenant has a kill switch that moves it to draft mode at once, and the engine does the same automatically if the error rate crosses its limit.
- If a stage gate cannot be passed after reasonable tuning, the product launches in draft mode for that customer or document type and keeps retrying the gate as data grows.
- Autonomy is granted per tenant and per document type, so one weak document type does not hold back the rest.

**What each gate needs measured**

- Field-level accuracy, and the share of vouchers posted with zero corrections.
- Wrong-ledger and wrong-tax-split errors, tracked separately because they are the costliest to unwind.
- Reversal count and time to detect an error.
- Review queue share and median review time.

**Product phases around the autonomy track**

The five product phases in section 12 continue to apply for scope: supplier invoices first, then matching and bank statements, then compliance documents and SAP, then the public API, then other industries. Autonomy stages 1 to 3 run during product phases 1 and 2, and stage 4 follows once phase 3 documents are covered.

**Research note: error rates for auto-posted vouchers**

No design partner has been consulted yet, so the gate numbers stay proposals until a Chartered Accountant partner agrees them. Published benchmarks cover extraction, not posting: vendors report 95 to 99 percent field accuracy, and strong accounts payable teams reach 49 to 89 percent touchless processing. Field accuracy overstates document accuracy. At 97 percent per field, only about 54 percent of 20-field invoices are fully correct.

So the autonomy gates should measure errors that reach Tally and are not reversed, not field accuracy. Starting proposal to test with real data: zero unreversed errors on high-value vouchers and under 0.5 percent of auto-posted vouchers needing correction overall. The partner CA sets the final figure.

**Open questions**

- The acceptable error rate for auto-posted vouchers, which should be agreed with design-partner customers and their CAs.
- The value cap for stage 2, and how it is set per customer.
- How many design partners and how many weeks of shadow mode are enough to pass the stage 1 gate.

## 12b. Pricing Model

Pricing should follow the value of work removed, not the number of users, because the product replaces data entry effort rather than adding screens for staff. The decided model is a voucher-based hybrid: a monthly platform fee that includes a bundle of vouchers, a per-voucher charge above the bundle, and a separate maintenance service for autonomy. Bank statements are priced separately, because one statement can hold hundreds of entries. No rupee figures are set yet; they need to be tested with design partners.

| Component | What it charges for | Why this shape |
| --- | --- | --- |
| Platform fee | Monthly fee per tenant, covering the bridge agent, review queue, monitoring dashboard and support | Gives predictable revenue and covers fixed costs of each customer |
| Per-voucher charge | Voucher bundle in the platform fee, then a charge per voucher posted to Tally above the bundle, with volume tiers; autonomously posted vouchers priced above those sent to review | Ties price to work delivered, since one document can create several vouchers, and rewards autonomy |
| Autonomy maintenance service | Monthly fee for accuracy monitoring, rule tuning, master data upkeep and pack updates, which is the paid service that comes with autonomy | Autonomy needs ongoing care; this fee funds it and is the extra revenue stream you identified |
| Onboarding fee | One-time setup: bridge agent install, master data mapping, and shadow mode period | Pays for the heavy first weeks, and filters out customers who are not serious |
| Integration usage for Ygudy products | Documents processed on behalf of Counsel, the CA firm product, or the admissions SaaS, billed to the calling product | Keeps internal products on the same meter as external customers |
| Bank statement charge | Separate pricing per page or per transaction line processed, outside the voucher bundle | One statement can hold hundreds of entries, so a per-voucher or per-file price would misstate the work |

**Pricing principles**

- Price against the cost of a data entry hire or outsourced entry per month, so the customer sees a clear saving.
- Failed or rejected documents are not charged, so the customer only pays for useful work.
- Autonomy is a paid tier, unlocked per document type as each gate in the roadmap is passed.
- A customer moved back to draft mode keeps paying the platform fee and the lower review-tier voucher rate.
- Annual plans offered at a discount, to reduce churn and fund onboarding.

**Cost inputs to model before setting prices**

- Model cost per document, which depends on page count, image quality and the model used.
- Cost of GSTN data lookups, if a paid source is used.
- Support and maintenance time per tenant during shadow mode and after.
- Cloud and storage cost for retained documents.

**Open questions**

- Decided: charge per voucher posted, not per document, since one document can produce several vouchers. Bank statements are the exception and get their own page or line-based pricing.
- How much to price the maintenance service as a share of the platform fee.
- Whether CA firms, who manage many clients, get a reseller or multi-client pricing.

## 12c. Tech Stack and Authentication

The platform runs on the same serverless stack as the other products, so the team reuses patterns and there is no new tooling to learn. Authentication adds one recommended choice and one product-specific piece, the identity of the bridge agent.

**Stack**

| Layer | Choice | Role in this product |
| --- | --- | --- |
| Database and storage | Supabase Postgres, with a schema per tenant, plus Supabase storage | Holds records, rules, review decisions and retained documents; pgvector supports matching and learning |
| Engine services | FastAPI on Cloud Run, with Pydantic AI for agents | Intake, extraction, validation, confidence routing and the connector calls |
| Background jobs | Inngest | Document pipelines, retries, scheduled master data syncs and the command queue to the bridge agent |
| Observability | Langfuse | Traces, cost per document and accuracy tracking for the autonomy gates |
| Web app | Next.js | Review queue, exceptions screen, activity log and monitoring dashboard |
| User authentication | WorkOS | Login, organisations, roles and single sign-on |

**Authentication decision**

Use WorkOS for user and organisation authentication. The customers are CA firms and manufacturers, who often want single sign-on and clear roles, and WorkOS provides organisations, roles and SSO without building them. Each WorkOS organisation maps to one tenant and one Postgres schema.

**Options considered**

| Option | Strength | Why it was not chosen |
| --- | --- | --- |
| Clerk | Fastest to add simple sign-up and login screens | Weaker fit for enterprise single sign-on and organisation features at this stage |
| Better Auth | Open source, self-hosted, no per-user cost | Enterprise features such as SSO would have to be built and maintained |
| Supabase Auth | Already part of the database vendor | Ties login to one vendor and is less suited to organisation-level roles |

**Roles**

- Owner: billing, autonomy settings, kill switch and user management.
- Reviewer: works the review queue, approves, edits, rejects and holds records.
- Viewer: read-only access to activity and reports, for example an auditor.
- Autonomy settings and the kill switch are limited to the owner role.

**Tenant isolation**

- The tenant schema is selected on the server from the WorkOS organisation in the signed session, never from anything the browser sends.
- Row-level security stays on as a second layer, so a bug in one query cannot cross tenants.
- Every action in the activity log records the user, the tenant and the time.

**Bridge agent identity**

The bridge agent is a machine, not a user, so it does not log in through WorkOS. Each agent gets its own signed credential, issued at install time and bound to one tenant and one Tally company. Credentials can be revoked from the cloud at once and are rotated on a schedule. This matches the security rules in Appendix E.

**Open questions**

- Whether WorkOS pricing at small-firm volumes fits the platform fee, or whether a cheaper option is needed for the smallest customers.
- Whether reviewers inside a CA firm need per-client access limits, so a staff member sees only some of the firm's clients.

## 13. Risks and Open Questions

- **Accuracy on poor-quality inputs:** WhatsApp photos and handwritten documents. Mitigation: conservative thresholds and the review queue.
- **Government API access and changes:** GSTN and portal rules change often. Mitigation: rules as configuration, with versioned packs.
- **Tally integration approach:** decided as a bridge agent on the customer's own PC, latest TallyPrime only. Remaining risk is the variability of Tally's local interface; see Appendix E.
- **Pricing model:** decided as a voucher-based hybrid with a separate bank statement price; see section 12b.
- **Pack authoring effort:** how much can be templated so packs take weeks, not months.
- **Data residency and consent** for documents containing personal data.
