"""The engine pipeline (section 5).

intake → extraction → validation → routing → posting → learning

One document is ingested here and every stage's result is written as it happens, so
the review queue, the audit trail and the accuracy dashboards all read from what
actually occurred rather than from a later reconstruction.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import psycopg

from engine.db import Database, J, TenantRef, record_activity
from engine.extraction.agent import ExtractionSchemaError, get_extraction_agent
from engine.extraction.classifier import classify_segment, split_documents
from engine.extraction.schemas import missing_mandatory
from engine.models import (
    ExtractedRecord,
    RoutingDecision,
    RoutingInput,
    RoutingReason,
    ValidationSummary,
)
from engine.observability import get_tracer
from engine.packs.loader import Pack, PackRegistry, get_registry
from engine.packs.models import RuleDefinition
from engine.posting.connectors import choose_target, get_connector
from engine.posting.connectors.base import PostContext
from engine.posting.mapper import build_voucher, conditions_for
from engine.repo import posting as posting_repo
from engine.repo import review as review_repo
from engine.repo import tenant as tenant_repo
from engine.routing.confidence import decide
from engine.validation.gstn import GstinStatus, get_gstn_client
from engine.validation.lookups import LookupBundle
from engine.validation.rules import RuleContext, RuleEngine

log = logging.getLogger("engine.pipeline")


@dataclass
class IngestResult:
    document_id: uuid.UUID
    status: str
    doc_type: str | None = None
    doc_type_confidence: float | None = None
    segments: list[dict[str, Any]] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    review_item_ids: list[uuid.UUID] = field(default_factory=list)
    voucher_ids: list[uuid.UUID] = field(default_factory=list)
    posted_autonomously: int = 0
    queued: int = 0
    needs_review: int = 0
    warnings: list[str] = field(default_factory=list)
    duplicate_of: uuid.UUID | None = None


# The review pane reads the first slice of a document; the full text is never
# needed in the database for a human to check a field.
PAGE_TEXT_LIMIT = 50_000

# Document types that are stored and matched against, but create no Tally voucher.
NO_VOUCHER_TARGETS = frozenset({"compliance_record", "po_record", "receipt_note"})


class Pipeline:
    def __init__(self, db: Database, registry: PackRegistry | None = None) -> None:
        self.db = db
        self.registry = registry or get_registry()
        self.gstn = get_gstn_client()

    def pack_for(self, tenant: TenantRef, version: str | None = None) -> Pack:
        return self.registry.get(tenant.pack_key, version)

    # ── intake ──────────────────────────────────────────────────────────────

    async def ingest(
        self,
        tenant: TenantRef,
        *,
        content: bytes | None = None,
        text: str | None = None,
        file_name: str | None = None,
        mime_type: str | None = None,
        channel: str = "web_upload",
        source_ref: str | None = None,
        sender: str | None = None,
        storage_path: str | None = None,
        pages: list[dict[str, Any]] | None = None,
        prefer_model: bool = False,
        trace_id: str | None = None,
    ) -> IngestResult:
        pack = self.pack_for(tenant)
        if text is None:
            if content is None:
                raise ValueError("either content or text is required")
            text = content.decode("utf-8", errors="replace")
        raw = content if content is not None else text.encode()
        file_hash = tenant_repo.content_hash(raw)
        if pages is None and text.strip():
            # Keep the page text so the reviewer sees the source beside the fields
            # (Appendix D) and a failed document can be re-read without asking the
            # sender for another copy.
            pages = [
                {
                    "page_no": 1,
                    "has_text_layer": True,
                    "text_digest": text[:PAGE_TEXT_LIMIT],
                    "meta": {"chars": len(text), "truncated": len(text) > PAGE_TEXT_LIMIT},
                }
            ]

        async with self.db.tenant(tenant) as cur:
            existing = await tenant_repo.find_document_by_hash(cur, file_hash)
            duplicate_of = existing["id"] if existing else None
            document = await tenant_repo.insert_document(
                cur,
                intake_channel=channel,
                file_name=file_name,
                mime_type=mime_type,
                file_size=len(raw),
                file_hash=file_hash,
                storage_path=storage_path,
                source_ref=source_ref,
                sender=sender,
                duplicate_of=duplicate_of,
                pack_key=pack.key,
                raw_meta={"trace_id": trace_id, "prefer_model": prefer_model},
            )
            document_id = document["id"]
            if pages:
                await tenant_repo.insert_pages(cur, document_id, pages)

            await record_activity(
                cur,
                tenant_id=tenant.id,
                action="document.received",
                actor_type="system",
                target_type="document",
                target_id=str(document_id),
                detail={
                    "channel": channel,
                    "file_name": file_name,
                    "duplicate_of": str(duplicate_of) if duplicate_of else None,
                },
            )

            if duplicate_of:
                await tenant_repo.set_document_status(
                    cur, document_id, "deduplicated", processed=True
                )
                await record_activity(
                    cur,
                    tenant_id=tenant.id,
                    action="document.deduplicated",
                    target_type="document",
                    target_id=str(document_id),
                    detail={"duplicate_of": str(duplicate_of)},
                )
                await tenant_repo.set_document_classification(
                    cur,
                    document_id,
                    doc_type=existing.get("doc_type"),
                    confidence=None,
                    pack_key=pack.key,
                    pack_version=pack.version,
                )
                return IngestResult(
                    document_id=document_id,
                    status="deduplicated",
                    doc_type=existing.get("doc_type"),
                    duplicate_of=duplicate_of,
                    warnings=["An identical file has already been received."],
                )

            result = await self._process(
                cur,
                tenant,
                document_id,
                text=text,
                pack=pack,
                prefer_model=prefer_model,
                trace_id=trace_id,
            )
        return result

    # ── extraction → validation → routing → posting ─────────────────────────

    async def _process(
        self,
        cur: psycopg.AsyncCursor,
        tenant: TenantRef,
        document_id: uuid.UUID,
        *,
        text: str,
        pack: Pack,
        prefer_model: bool,
        trace_id: str | None,
    ) -> IngestResult:
        agent = get_extraction_agent(pack)
        result = IngestResult(document_id=document_id, status="received")

        segments = split_documents(text, pack)
        result.segments = [
            {"doc_type": s.doc_type, "confidence": s.doc_type_confidence, "index": s.order_index}
            for s in segments
        ]
        if segments:
            result.doc_type = segments[0].doc_type
            result.doc_type_confidence = segments[0].doc_type_confidence

        await tenant_repo.set_document_classification(
            cur,
            document_id,
            doc_type=result.doc_type,
            confidence=result.doc_type_confidence,
            pack_key=pack.key,
            pack_version=pack.version,
            page_count=len(segments),
        )

        if not segments or result.doc_type in (None, "unknown"):
            await tenant_repo.set_document_status(
                cur,
                document_id,
                "exception",
                exception_reason=(
                    "The document could not be classified against this tenant's packs. "
                    "A person should check the file."
                ),
                processed=True,
            )
            result.status = "exception"
            result.warnings.append("The document type could not be identified.")
            await record_activity(
                cur,
                tenant_id=tenant.id,
                action="document.exception",
                target_type="document",
                target_id=str(document_id),
                detail={"reason": "unclassified"},
                severity="warn",
            )
            return result

        await tenant_repo.set_document_status(cur, document_id, "extracting")
        run_id: uuid.UUID | None = None
        extracted_total = 0

        for segment in segments:
            if segment.doc_type in (None, "unknown"):
                segment.doc_type = result.doc_type
            run_id = await tenant_repo.start_extraction_run(
                cur,
                document_id=document_id,
                pack_key=pack.key,
                pack_version=pack.version,
                model=None,
                prompt_version=None,
                trace_id=trace_id,
            )
            try:
                outcome = await agent.extract(segment, prefer_model=prefer_model)
            except ExtractionSchemaError as exc:
                await tenant_repo.finish_extraction_run(
                    cur, run_id, status="schema_failed", schema_ok=False, error_text=str(exc)
                )
                await tenant_repo.set_document_status(
                    cur,
                    document_id,
                    "exception",
                    exception_reason=str(exc),
                    processed=True,
                )
                result.status = "exception"
                result.warnings.append(str(exc))
                await record_activity(
                    cur,
                    tenant_id=tenant.id,
                    action="extraction.failed",
                    target_type="document",
                    target_id=str(document_id),
                    detail={"error": str(exc)},
                    severity="error",
                )
                return result

            await tenant_repo.finish_extraction_run(
                cur,
                run_id,
                status="succeeded" if outcome.schema_ok else "escalated",
                schema_ok=outcome.schema_ok,
                record_count=len(outcome.records),
                tokens_in=outcome.usage.get("tokens_in"),
                tokens_out=outcome.usage.get("tokens_out"),
                cost_usd=outcome.usage.get("cost_usd"),
                error_text=outcome.error,
            )
            extracted_total += len(outcome.records)
            for record in outcome.records:
                record.order_index = len(result.records)
                await self._handle_record(cur, tenant, document_id, run_id, record, pack, result)

        if extracted_total == 0:
            await tenant_repo.set_document_status(
                cur,
                document_id,
                "exception",
                exception_reason="No document content could be read from this file.",
                processed=True,
            )
            result.status = "exception"
            return result

        status = "posted" if result.posted_autonomously else (
            "in_review" if result.needs_review else "routing"
        )
        await tenant_repo.set_document_status(cur, document_id, status, processed=True)
        result.status = status
        return result

    async def _handle_record(
        self,
        cur: psycopg.AsyncCursor,
        tenant: TenantRef,
        document_id: uuid.UUID,
        run_id: uuid.UUID | None,
        record: ExtractedRecord,
        pack: Pack,
        result: IngestResult,
    ) -> None:
        spec = pack.document(record.doc_type)
        missing = missing_mandatory(spec, record.payload)
        if missing:
            record.schema_ok = False
            record.payload["_missing_mandatory"] = missing

        row = await tenant_repo.insert_record(
            cur, document_id=document_id, run_id=run_id, record=record
        )
        await tenant_repo.insert_field_values(cur, row["id"], record.fields)
        await tenant_repo.link_side_tables(cur, row, record)

        if not record.schema_ok:
            summary = ValidationSummary()
        else:
            lookups = await self._build_lookups(
                cur, tenant, record, pack, exclude_record_id=row["id"]
            )
            context = await self._rule_context(cur, tenant, pack)
            summary = RuleEngine(pack).evaluate(record, context, lookups)
            await tenant_repo.insert_rule_results(cur, row["id"], summary, pack)

        decision = decide(await self._routing_input(cur, tenant, record, summary, row))

        entry: dict[str, Any] = {"decision": decision.decision, "record_id": str(row["id"])}
        result.records.append(entry)

        if decision.decision == "auto_post":
            voucher = await self.post_record(cur, tenant, row, record, decision, pack, result)
            entry["voucher_id"] = str(voucher["id"]) if voucher else None
            entry["status"] = voucher["status"] if voucher else "skipped"
            await review_repo.record_auto_post_decision(
                cur, record_row=row, record=record, decision=decision
            )
            if decision.posting_mode == "shadow":
                result.warnings.append(
                    "Shadow mode: the record was measured but not posted to Tally."
                )
            else:
                result.posted_autonomously += 1
            return

        item = await review_repo.insert_review_item(
            cur, record_row=row, record=record, decision=decision
        )
        result.review_item_ids.append(item["id"])
        result.needs_review += 1
        entry["review_item_id"] = str(item["id"])
        entry["reasons"] = [reason.message for reason in decision.reasons][:4]
        await tenant_repo.set_record_status(cur, row["id"], "in_review")
        await record_activity(
            cur,
            tenant_id=tenant.id,
            action="record.routed_to_review",
            target_type="record",
            target_id=str(row["id"]),
            detail={
                "doc_type": record.doc_type,
                "band": decision.priority_band,
                "reasons": [reason.code for reason in decision.reasons],
            },
            severity="info" if decision.priority_band == "low_confidence" else "warn",
        )

    # ── helpers ─────────────────────────────────────────────────────────────

    async def _rule_context(
        self, cur: psycopg.AsyncCursor, tenant: TenantRef, pack: Pack
    ) -> RuleContext:
        stored = await review_repo.rule_settings(cur)
        rule_settings = {
            key: value.get("settings", {}) for key, value in stored.items() if value.get("settings")
        }
        enabled = {key: bool(value.get("enabled", True)) for key, value in stored.items()}
        overrides = {
            key: value["severity_override"]
            for key, value in stored.items()
            if value.get("severity_override")
        }
        return RuleContext(
            pack=pack,
            tenant_id=tenant.id,
            today=dt.date.today(),
            rule_settings=rule_settings,
            enabled=enabled,
            severity_overrides=overrides,
            e_invoicing_applies=tenant.e_invoicing_applies,
        )

    async def _build_lookups(
        self,
        cur: psycopg.AsyncCursor,
        tenant: TenantRef,
        record: ExtractedRecord,
        pack: Pack,
        *,
        exclude_record_id: uuid.UUID | None = None,
    ) -> LookupBundle:
        statuses = await self._gstin_statuses(cur, record, pack)
        return await tenant_repo.build_lookups(
            cur,
            record,
            gstn_status=statuses,
            e_invoicing_applies=tenant.e_invoicing_applies,
            # The record being validated is already stored, so it must not count
            # as its own duplicate.
            exclude_record_id=exclude_record_id,
        )

    async def _gstin_statuses(
        self, cur: psycopg.AsyncCursor, record: ExtractedRecord, pack: Pack
    ) -> dict[str, GstinStatus]:
        rules = pack.rules_for(record.doc_type)
        wanted: set[str] = set()
        for rule in rules:
            if rule.requires_lookup != "gstn":
                continue
            for name in rule.fields:
                value = record.payload.get(name)
                if isinstance(value, str) and value.strip():
                    wanted.add(value.strip().upper())

        if not wanted:
            return {}

        await cur.execute(
            "select * from public.gstin_lookup_cache "
            "where gstin = any(%s) and expires_at > now()",
            (list(wanted),),
        )
        cached = {row["gstin"]: row for row in await cur.fetchall()}

        result: dict[str, GstinStatus] = {}
        for gstin in wanted:
            row = cached.get(gstin)
            if row:
                result[gstin] = GstinStatus(
                    gstin=gstin,
                    state=row["status"] or "unknown",
                    legal_name=row["legal_name"],
                    taxpayer_type=row["taxpayer_type"],
                    source=row["source"],
                    degraded=bool(row["degraded"]),
                    detail="cached lookup",
                    raw=row["raw"] or {},
                )
                continue
            status = await self.gstn.status(gstin)
            result[gstin] = status
            await cur.execute(
                """
                insert into public.gstin_lookup_cache
                  (gstin, legal_name, trade_name, status, taxpayer_type, source, degraded, raw,
                   checked_at, expires_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, now(), now() + make_interval(hours => 24))
                on conflict (gstin) do update
                  set status = excluded.status, legal_name = excluded.legal_name,
                      trade_name = excluded.trade_name, taxpayer_type = excluded.taxpayer_type,
                      source = excluded.source, degraded = excluded.degraded, raw = excluded.raw,
                      checked_at = now(), expires_at = excluded.expires_at
                """,
                (
                    gstin,
                    status.legal_name,
                    status.trade_name,
                    status.state,
                    status.taxpayer_type,
                    status.source,
                    status.degraded,
                    J(status.raw or {}),
                ),
            )
        return result

    async def _routing_input(
        self,
        cur: psycopg.AsyncCursor,
        tenant: TenantRef,
        record: ExtractedRecord,
        summary: ValidationSummary,
        row: dict[str, Any],
    ) -> RoutingInput:
        mapping = await tenant_repo.load_ledger_mapping(
            cur, gstin=record.payload.get("vendor_gstin"), name=record.payload.get("vendor_name")
        )
        used_today = await posting_repo.auto_posted_today(cur)
        value = row.get("amount_total")
        return RoutingInput(
            record=record,
            validation=summary,
            confidence_threshold=tenant.confidence_threshold,
            autopost_value_cap=tenant.autopost_value_cap,
            posting_mode=tenant.effective_posting_mode,  # type: ignore[arg-type]
            autonomy_stage=tenant.autonomy_stage,
            is_known_vendor=bool(mapping and mapping.is_known_vendor),
            ledger_mapping_confidence=mapping.confidence if mapping else None,
            daily_autopost_used=used_today,
            daily_autopost_cap=tenant.daily_autopost_cap,
            duplicate_exact=bool(summary and any(
                r.rule_key == "duplicate_invoice" and r.outcome == "fail" for r in summary.results
            )),
            document_value=Decimal(str(value)) if value is not None else None,
            warns_allow_autopost=tenant.warns_allow_autopost,
            approval_limit=(
                Decimal(str(tenant.settings["reviewer_approval_limit"]))
                if tenant.settings.get("reviewer_approval_limit")
                else None
            ),
        )

    async def post_record(
        self,
        cur: psycopg.AsyncCursor,
        tenant: TenantRef,
        row: dict[str, Any],
        record: ExtractedRecord,
        decision: RoutingDecision,
        pack: Pack,
        result: IngestResult | None = None,
    ) -> dict[str, Any] | None:
        """Build and send one voucher. Public because approving a queued record
        goes through exactly the same path as an automatic post."""
        # Approving a queued record reuses this path, so there is no ingest result
        # to collect counters on; a minimal one keeps the flow identical.
        result = result if result is not None else IngestResult(
            document_id=row["document_id"], status="approved"
        )
        spec = pack.document(record.doc_type)
        # Compliance documents and the records used for matching never become a
        # voucher (Appendix C): approving one closes the review item, nothing posts.
        if spec is not None and spec.posts_to in NO_VOUCHER_TARGETS:
            return None
        mapping = await tenant_repo.load_ledger_mapping(
            cur, gstin=record.payload.get("vendor_gstin"), name=record.payload.get("vendor_name")
        )
        agent = await self._active_agent(tenant)
        mapping_result = build_voucher(
            record,
            pack=pack,
            tenant=tenant,
            record_id=row["id"],
            business_key=row.get("business_key"),
            mapping=mapping,
            condition=conditions_for(record, mapping, tenant),
            tally_company=(agent or {}).get("tally_company"),
        )
        draft = mapping_result.draft

        existing = await posting_repo.find_voucher_by_idempotency(cur, draft.idempotency_key)
        if existing:
            return existing

        target = choose_target(tenant, mode=decision.posting_mode, agent_available=bool(agent))
        connector = get_connector(target, cur=cur)
        ctx = PostContext(
            tenant=tenant,
            record=record,
            record_row=row,
            draft=draft,
            agent=agent,
            mode=decision.posting_mode,
        )
        problems = await connector.validate(ctx)
        if problems:
            # A voucher that cannot be sent is not a failure of the record: the
            # reviewer sees the destination's problem in plain language.
            item = await review_repo.insert_review_item(
                cur,
                record_row=row,
                record=record,
                decision=decision.model_copy(
                    update={
                        "decision": "review",
                        "posting_mode": "draft",
                        "priority_band": "rule_failure",
                        "priority": min(decision.priority, 12),
                        # The destination's own words, so the reviewer knows what
                        # to fix rather than guessing why the post did not happen.
                        "reasons": list(decision.reasons)
                        + [
                            RoutingReason(
                                code="destination_rejected",
                                message="The destination could not accept this voucher: "
                                + " ".join(problems),
                            )
                        ],
                    }
                ),
            )
            await review_repo.insert_review_action(
                cur,
                item_id=item["id"],
                record_id=row["id"],
                actor="engine",
                actor_role="system",
                action="hold",
                note="Connector could not send this voucher: " + " ".join(problems),
            )
            result.review_item_ids.append(item["id"])
            result.needs_review += 1
            result.warnings.extend(problems)
            await tenant_repo.set_record_status(cur, row["id"], "in_review")
            return None

        status = {
            "autonomous": "queued",
            "draft": "draft_staged",
            "shadow": "shadow",
        }[decision.posting_mode]

        voucher = await posting_repo.insert_voucher(
            cur,
            tenant=tenant,
            record_row=row,
            record=record,
            draft=draft,
            status=status,
        )
        ctx.voucher_id = voucher["id"]

        attempt_id = await posting_repo.start_posting_attempt(
            cur,
            voucher_id=voucher["id"],
            mode=decision.posting_mode,
            connector=connector.name,
            request={"voucher_type": draft.voucher_type, "target": target},
        )
        post_result = await connector.post(ctx)

        await posting_repo.finish_posting_attempt(
            cur,
            attempt_id,
            outcome=(
                "success"
                if post_result.ok
                else ("tally_rejected" if post_result.status == "tally_rejected" else "transport_error")
            ),
            response=post_result.response,
            error_code=post_result.error_code,
            error_text=post_result.error_text,
        )

        final_status = {
            "posted": "posted",
            "queued": "queued",
            "staged": "draft_staged",
            "shadow": "shadow",
        }.get(post_result.status, "failed")

        await posting_repo.set_voucher_status(
            cur,
            voucher["id"],
            final_status,
            tally_voucher_number=post_result.tally_voucher_number,
            tally_voucher_id=post_result.tally_voucher_id,
            error_text=post_result.error_text,
            posted=post_result.status == "posted",
            staged_at=post_result.status == "staged",
        )
        await posting_repo.link_compliance_to_voucher(
            cur, record_id=row["id"], voucher_id=voucher["id"]
        )
        if post_result.ok and post_result.status != "shadow":
            await posting_repo.set_record_status_for_voucher(
                cur,
                voucher["id"],
                "posted" if post_result.status == "posted" else "posted_draft",
            )
        elif post_result.ok:
            await posting_repo.set_record_status_for_voucher(cur, voucher["id"], "auto_posted")
        else:
            await posting_repo.set_record_status_for_voucher(cur, voucher["id"], "failed")

        await record_activity(
            cur,
            tenant_id=tenant.id,
            action=f"voucher.{final_status}",
            actor_type="system",
            target_type="voucher",
            target_id=str(voucher["id"]),
            detail={
                "connector": connector.name,
                "mode": decision.posting_mode,
                "idempotency_key": draft.idempotency_key,
                "error": post_result.error_text,
            },
            severity="info" if post_result.ok else "error",
        )
        result.voucher_ids.append(voucher["id"])
        if post_result.status == "queued":
            result.queued += 1
        if post_result.error_text:
            result.warnings.append(post_result.error_text)
        if mapping_result.warnings:
            result.warnings.extend(mapping_result.warnings)
        return voucher

    async def _active_agent(self, tenant: TenantRef) -> dict[str, Any] | None:
        """The agent bound to this tenant, if one is online (Appendix E)."""
        from engine.posting.commands import active_agent

        async with self.db.control() as cur:
            return await active_agent(cur, tenant.id)
