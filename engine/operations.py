"""Operations the web app and the bridge agent call.

Everything here takes a resolved ``TenantRef`` — never a tenant name, schema or
organisation id from the caller — so the tenant is always decided on the server
from the signed session (section 12c).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any, Sequence


from engine.db import Database, J, TenantRef, record_activity
from engine.models import RoutingDecision, RoutingReason
from engine.packs.loader import PackRegistry, get_registry
from engine.pipeline import Pipeline
from engine.posting.connectors import get_connector
from engine.repo import posting as posting_repo
from engine.repo import review as review_repo
from engine.repo import tenant as tenant_repo
from engine.routing.autonomy import describe_stage, evaluate_gate, should_auto_demote
from engine.validation.rules import RuleEngine


class TenantScopeError(RuntimeError):
    """Raised when an operation would cross tenants or touch another's data."""


class EngineOperations:
    def __init__(
        self, db: Database, registry: PackRegistry | None = None, pipeline: Pipeline | None = None
    ) -> None:
        self.db = db
        self.registry = registry or get_registry()
        self.pipeline = pipeline or Pipeline(db, self.registry)

    def pack_for(self, tenant: TenantRef):
        return self.registry.get(tenant.pack_key)

    # ── review queue (Appendix D) ───────────────────────────────────────────

    async def queue(self, tenant: TenantRef, **filters: Any) -> dict[str, Any]:
        async with self.db.tenant(tenant) as cur:
            items = await review_repo.queue_list(cur, **filters)
            counts = await review_repo.queue_counts(cur)
        return {"items": items, "counts": counts}

    async def item(self, tenant: TenantRef, item_id: uuid.UUID) -> dict[str, Any] | None:
        async with self.db.tenant(tenant) as cur:
            detail = await review_repo.review_item_detail(cur, item_id)
            if detail is None:
                return None
            pack = self.pack_for(tenant)
            record_row = detail["record"]
            spec = pack.document(record_row["doc_type"]) if record_row else None
            detail["voucher_preview"] = await self._preview(cur, tenant, record_row, pack)
            detail["pack"] = {
                "key": pack.key,
                "version": pack.version,
                "doc_type_label": spec.label if spec else None,
                "mandatory_fields": spec.mandatory_fields if spec else [],
            }
        return detail

    async def _preview(self, cur, tenant: TenantRef, record_row: dict[str, Any], pack) -> dict | None:
        from engine.extraction.agent import ExtractionAgent
        from engine.posting.mapper import build_voucher, conditions_for

        if not record_row:
            return None
        record = _record_from_row(record_row)
        mapping = await tenant_repo.load_ledger_mapping(
            cur, gstin=record_row.get("vendor_gstin"), name=record_row.get("vendor_name")
        )
        result = build_voucher(
            record,
            pack=pack,
            tenant=tenant,
            record_id=record_row["id"],
            business_key=record_row.get("business_key"),
            mapping=mapping,
            condition=conditions_for(record, mapping, tenant),
        )
        return {
            "voucher": result.draft.model_dump(mode="json"),
            "warnings": result.warnings,
            "proposals": result.proposals,
            "balanced": result.draft.totals_balanced(),
        }

    async def approve(
        self,
        tenant: TenantRef,
        item_id: uuid.UUID,
        *,
        actor: str,
        actor_role: str,
        note: str | None = None,
        edits: dict[str, str] | None = None,
        one_off_fields: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Approve, or edit and approve. Corrections feed the learning loop."""
        pack = self.pack_for(tenant)
        async with self.db.tenant(tenant) as cur:
            detail = await review_repo.review_item_detail(cur, item_id)
            if detail is None:
                raise TenantScopeError(f"review item {item_id} is not in this tenant")
            item = detail["item"]
            record_row = detail["record"]
            record = _record_from_row(record_row)

            corrections: list[dict[str, Any]] = []
            for path, new_value in (edits or {}).items():
                old_value = _lookup_path(record.payload, path)
                if str(old_value) == str(new_value):
                    continue
                _set_path(record.payload, path, new_value)
                await cur.execute(
                    """
                    update field_values set value_text = %s, corrected = true, corrected_by = %s,
                           corrected_at = now(), confidence = 1.0
                     where record_id = %s and path = %s
                    """,
                    (new_value, actor, record_row["id"], path),
                )
                await review_repo.insert_review_action(
                    cur,
                    item_id=item_id,
                    record_id=record_row["id"],
                    actor=actor,
                    actor_role=actor_role,
                    action="correct",
                    field_path=path,
                    before_value=None if old_value is None else str(old_value),
                    after_value=new_value,
                    is_one_off=path in one_off_fields,
                )
                corrections.append({"path": path, "before": old_value, "after": new_value})

            if corrections:
                await cur.execute(
                    "update extracted_records set payload = %s, status = 'corrected' where id = %s",
                    (J(record.payload), record_row["id"]),
                )

            summary = await self._revalidate(cur, tenant, pack, record, record_row)

            # A reviewer's approval is the reason this record is posting. Carrying
            # it on the decision means any later review row explains itself.
            decision = RoutingDecision(
                decision="auto_post",
                posting_mode="draft",
                reasons=[
                    RoutingReason(
                        code="approved_by_reviewer",
                        message=f"Approved by {actor_role or 'a reviewer'} after review.",
                    )
                ],
                priority=item["priority"],
                priority_band=item["priority_band"],
            )
            voucher = await self.pipeline.post_record(
                cur, tenant, record_row, record, decision, pack
            )

            status = "corrected" if corrections else "approved"
            await review_repo.resolve_review_item(
                cur,
                item_id,
                status=status,
                resolution="edit_and_approve" if corrections else "approve",
                reason=None,
                note=note,
                actor=actor,
            )
            if not corrections:
                await review_repo.insert_review_action(
                    cur,
                    item_id=item_id,
                    record_id=record_row["id"],
                    actor=actor,
                    actor_role=actor_role,
                    action="approve",
                    note=note,
                )

            for correction in corrections:
                await review_repo.insert_correction(
                    cur,
                    record_id=record_row["id"],
                    document_id=record_row.get("document_id"),
                    field_path=correction["path"],
                    before_value=None if correction["before"] is None else str(correction["before"]),
                    after_value=str(correction["after"]),
                    vendor_gstin=record_row.get("vendor_gstin"),
                    vendor_name=record_row.get("vendor_name"),
                    doc_type=record_row.get("doc_type"),
                    actor=actor,
                    is_one_off=correction["path"] in one_off_fields,
                )
                await self._learn_from_correction(cur, tenant, record_row, correction, actor)

            await tenant_repo.set_record_status(
                cur, record_row["id"], "approved" if not corrections else "corrected"
            )
            await record_activity(
                cur,
                tenant_id=tenant.id,
                action="review.approved" if not corrections else "review.corrected",
                actor_type="user",
                actor_id=actor,
                target_type="record",
                target_id=str(record_row["id"]),
                detail={
                    "item_id": str(item_id),
                    "edits": [c["path"] for c in corrections],
                    "note": note,
                    "voucher_id": str(voucher["id"]) if voucher else None,
                },
            )

        return {
            "item_status": status,
            "voucher_id": voucher["id"] if voucher else None,
            "voucher_status": voucher["status"] if voucher else None,
            "corrections": corrections,
            "rule_summary": _rule_summary(summary),
        }

    async def _revalidate(self, cur, tenant: TenantRef, pack, record, record_row):
        lookups = await self.pipeline._build_lookups(
            cur, tenant, record, pack, exclude_record_id=record_row["id"]
        )
        context = await self.pipeline._rule_context(cur, tenant, pack)
        summary = RuleEngine(pack).evaluate(record, context, lookups)
        await tenant_repo.insert_rule_results(cur, record_row["id"], summary, pack)
        return summary

    async def _learn_from_correction(
        self, cur, tenant: TenantRef, record_row: dict[str, Any], correction: dict[str, Any], actor: str
    ) -> None:
        """Repeated identical corrections prompt a tenant rule for a supervisor
        to confirm (6.6). One-off corrections are stored but never learned."""
        if correction["path"] not in {"party_ledger", "purchase_ledger", "expense_ledger"}:
            return
        count = await review_repo.count_repeated_corrections(
            cur, vendor_gstin=record_row.get("vendor_gstin"), field_path=correction["path"]
        )
        if count < 3:
            return
        field = {
            "party_ledger": "target_party_ledger",
            "purchase_ledger": "purchase_ledger",
            "expense_ledger": "expense_ledger",
        }[correction["path"]]
        await review_repo.upsert_rule_suggestion(
            cur,
            kind="ledger_mapping",
            vendor_gstin=record_row.get("vendor_gstin"),
            doc_type=record_row.get("doc_type"),
            field_path=correction["path"],
            proposal={
                f"{field}": correction["after"],
                "occurrences": count,
                "actor": actor,
                "message": (
                    f"{count} corrections mapped this vendor's {correction['path']} to "
                    f"{correction['after']}. Turn it into a tenant rule?"
                ),
            },
        )

    async def reject(
        self,
        tenant: TenantRef,
        item_id: uuid.UUID,
        *,
        actor: str,
        actor_role: str,
        reason: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        async with self.db.tenant(tenant) as cur:
            detail = await review_repo.review_item_detail(cur, item_id)
            if detail is None:
                raise TenantScopeError("review item not found in this tenant")
            record_row = detail["record"]
            await review_repo.insert_review_action(
                cur,
                item_id=item_id,
                record_id=record_row["id"],
                actor=actor,
                actor_role=actor_role,
                action="reject",
                note=f"{reason}: {note}" if note else reason,
            )
            await review_repo.resolve_review_item(
                cur,
                item_id,
                status="rejected",
                resolution="reject",
                reason=reason,
                note=note,
                actor=actor,
            )
            await tenant_repo.set_record_status(cur, record_row["id"], "rejected")
            await record_activity(
                cur,
                tenant_id=tenant.id,
                action="review.rejected",
                actor_type="user",
                actor_id=actor,
                target_type="record",
                target_id=str(record_row["id"]),
                detail={"item_id": str(item_id), "reason": reason, "note": note},
                severity="warn",
            )
        return {"item_status": "rejected"}

    async def hold(
        self,
        tenant: TenantRef,
        item_id: uuid.UUID,
        *,
        actor: str,
        actor_role: str,
        note: str,
    ) -> dict[str, Any]:
        async with self.db.tenant(tenant) as cur:
            detail = await review_repo.review_item_detail(cur, item_id)
            if detail is None:
                raise TenantScopeError("review item not found in this tenant")
            await review_repo.hold_review_item(cur, item_id, note=note, actor=actor)
            await review_repo.insert_review_action(
                cur,
                item_id=item_id,
                record_id=detail["record"]["id"],
                actor=actor,
                actor_role=actor_role,
                action="hold",
                note=note,
            )
            await tenant_repo.set_record_status(cur, detail["record"]["id"], "held")
            await record_activity(
                cur,
                tenant_id=tenant.id,
                action="review.held",
                actor_type="user",
                actor_id=actor,
                target_type="review_item",
                target_id=str(item_id),
                detail={"note": note},
            )
        return {"item_status": "held"}

    async def request_document(
        self,
        tenant: TenantRef,
        item_id: uuid.UUID,
        *,
        actor: str,
        actor_role: str,
        channel: str = "email",
        template_key: str = "clearer_copy",
        message: str | None = None,
    ) -> dict[str, Any]:
        async with self.db.tenant(tenant) as cur:
            detail = await review_repo.review_item_detail(cur, item_id)
            if detail is None:
                raise TenantScopeError("review item not found in this tenant")
            document = detail["document"]
            body = message or _clarification_template(template_key, document)
            request = await review_repo.insert_clarification_request(
                cur,
                item_id=item_id,
                document_id=document["id"],
                channel=channel,
                to_address=document.get("sender"),
                template_key=template_key,
                body=body,
            )
            await review_repo.insert_review_action(
                cur,
                item_id=item_id,
                record_id=detail["record"]["id"],
                actor=actor,
                actor_role=actor_role,
                action="request_document",
                note=f"{channel}: {body[:200]}",
            )
            await cur.execute(
                "update review_items set status = 'clarification_requested' where id = %s",
                (item_id,),
            )
            await record_activity(
                cur,
                tenant_id=tenant.id,
                action="review.clarification_requested",
                actor_type="user",
                actor_id=actor,
                target_type="review_item",
                target_id=str(item_id),
                detail={"channel": channel, "to": document.get("sender")},
            )
        return {"item_status": "clarification_requested", "request_id": request["id"]}

    async def reassign(
        self,
        tenant: TenantRef,
        item_id: uuid.UUID,
        *,
        actor: str,
        actor_role: str,
        assignee: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        async with self.db.tenant(tenant) as cur:
            detail = await review_repo.review_item_detail(cur, item_id)
            if detail is None:
                raise TenantScopeError("review item not found in this tenant")
            await review_repo.assign_review_item(cur, item_id, assignee=assignee, actor=actor)
            await review_repo.insert_review_action(
                cur,
                item_id=item_id,
                record_id=detail["record"]["id"],
                actor=actor,
                actor_role=actor_role,
                action="reassign",
                note=note or f"assigned to {assignee}",
            )
        return {"item_status": "assigned", "assignee": assignee}

    async def bulk_approve(
        self,
        tenant: TenantRef,
        item_ids: Sequence[uuid.UUID],
        *,
        actor: str,
        actor_role: str,
        note: str | None = None,
        group_by: str | None = None,
    ) -> dict[str, Any]:
        """Clear many low-risk records at once, grouped by vendor or reason."""
        approved: list[str] = []
        skipped: list[dict[str, Any]] = []
        async with self.db.tenant(tenant) as cur:
            items = await review_repo.queue_list(cur, statuses=("pending",), limit=500)
            wanted = {str(i) for i in item_ids}
            order = {str(item["id"]): item for item in items}
            for item_id in wanted:
                item = order.get(item_id)
                if item is None:
                    skipped.append({"id": item_id, "reason": "not pending in this tenant"})
                    continue
                if item["priority_band"] == "rule_failure":
                    skipped.append(
                        {
                            "id": item_id,
                            "reason": "a rule failed, so it needs individual attention",
                            "rules": item.get("failed_rules") or [],
                        }
                    )
                    continue
                if item.get("requires_supervisor") and actor_role != "owner":
                    skipped.append(
                        {"id": item_id, "reason": "above this reviewer's approval limit"}
                    )
                    continue
                approved.append(item_id)

        results = []
        for item_id in approved:
            results.append(
                await self.approve(
                    tenant, uuid.UUID(item_id), actor=actor, actor_role=actor_role, note=note
                )
            )
        async with self.db.tenant(tenant) as cur:
            await record_activity(
                cur,
                tenant_id=tenant.id,
                action="review.bulk_approved",
                actor_type="user",
                actor_id=actor,
                target_type="review_item",
                detail={
                    "count": len(approved),
                    "group_by": group_by,
                    "skipped": len(skipped),
                },
            )
        return {"approved": approved, "skipped": skipped, "results": results}

    # ── vouchers ────────────────────────────────────────────────────────────

    async def voucher(self, tenant: TenantRef, voucher_id: uuid.UUID) -> dict[str, Any] | None:
        async with self.db.tenant(tenant) as cur:
            await cur.execute("select * from vouchers where id = %s", (voucher_id,))
            voucher = await cur.fetchone()
            if not voucher:
                return None
            await cur.execute(
                "select * from posting_attempts where voucher_id = %s order by attempt, started_at",
                (voucher_id,),
            )
            attempts = list(await cur.fetchall())
            await cur.execute(
                "select * from compliance_records where voucher_id = %s", (voucher_id,)
            )
            compliance = list(await cur.fetchall())
        return {"voucher": voucher, "attempts": attempts, "compliance": compliance}

    async def reverse_voucher(
        self, tenant: TenantRef, voucher_id: uuid.UUID, *, actor: str, reason: str
    ) -> dict[str, Any]:
        """Reversal is a command like any other (Appendix E)."""
        pack = self.pack_for(tenant)
        async with self.db.tenant(tenant) as cur:
            await cur.execute("select * from vouchers where id = %s", (voucher_id,))
            voucher = await cur.fetchone()
            if voucher is None:
                raise TenantScopeError("voucher not found in this tenant")
            if voucher["status"] not in {"posted", "queued", "draft_staged"}:
                raise TenantScopeError(
                    f"a voucher in status {voucher['status']} cannot be reversed"
                )
            await cur.execute(
                "select * from extracted_records where id = %s", (voucher["record_id"],)
            )
            record_row = await cur.fetchone()
            record = _record_from_row(record_row)
            draft = _draft_from_voucher(voucher)
            agent = await self.pipeline._active_agent(tenant)
            connector = get_connector("tally_bridge", cur=cur)
            from engine.posting.connectors.base import PostContext, PostResult

            ctx = PostContext(
                tenant=tenant,
                record=record,
                record_row=record_row,
                draft=draft,
                voucher_id=voucher_id,
                agent=agent,
                mode="draft",
            )
            result = await connector.roll_back(
                ctx,
                PostResult(
                    ok=True,
                    status="posted",
                    connector=connector.name,
                    response={
                        "voucher_number": voucher["tally_voucher_number"],
                        "voucher_id": voucher["tally_voucher_id"],
                    },
                ),
            )
            await posting_repo.set_voucher_status(
                cur,
                voucher_id,
                "reversed" if result.ok else "failed",
                error_text=result.error_text,
                reversed=result.ok,
            )
            await tenant_repo.set_record_status(cur, voucher["record_id"], "reversed")
            await record_activity(
                cur,
                tenant_id=tenant.id,
                action="voucher.reversed",
                actor_type="user",
                actor_id=actor,
                target_type="voucher",
                target_id=str(voucher_id),
                detail={"reason": reason, "queued": result.ok, "error": result.error_text},
                severity="warn",
            )
        return {
            "voucher_status": "reversed" if result.ok else "failed",
            "command": result.response,
            "error": result.error_text,
        }

    async def export(
        self, tenant: TenantRef, voucher_ids: Sequence[uuid.UUID], *, fmt: str = "csv"
    ) -> dict[str, Any]:
        """Mode B: generate a Tally-compatible import file (Appendix C)."""
        from engine.posting.connectors.tally import envelope, draft_to_csv_rows, rows_to_csv

        pack = self.pack_for(tenant)
        drafts = []
        async with self.db.tenant(tenant) as cur:
            for voucher_id in voucher_ids:
                await cur.execute("select * from vouchers where id = %s", (voucher_id,))
                voucher = await cur.fetchone()
                if voucher is None:
                    continue
                drafts.append(_draft_from_voucher(voucher))
        if fmt == "csv":
            rows: list[dict[str, str]] = []
            for draft in drafts:
                rows.extend(draft_to_csv_rows(draft))
            return {"format": "csv", "content": rows_to_csv(rows), "count": len(drafts)}
        company = drafts[0].tally_company if drafts else None
        return {
            "format": "tally_xml",
            "content": envelope(drafts, company=company),
            "count": len(drafts),
            "pack_version": pack.version,
        }

    # ── master data ─────────────────────────────────────────────────────────

    async def master_proposals(self, tenant: TenantRef) -> list[dict[str, Any]]:
        async with self.db.tenant(tenant) as cur:
            return await posting_repo.pending_master_proposals(cur)

    async def decide_master_proposal(
        self, tenant: TenantRef, proposal_id: uuid.UUID, *, approve: bool, actor: str
    ) -> dict[str, Any]:
        async with self.db.tenant(tenant) as cur:
            proposal = await posting_repo.decide_master_proposal(
                cur, proposal_id, approve=approve, actor=actor
            )
            if proposal is None:
                raise TenantScopeError("proposal not found in this tenant")
            if approve and proposal["kind"] == "party":
                await tenant_repo.upsert_ledger_mapping(
                    cur,
                    gstin=proposal["gstin"],
                    name=proposal["proposed_name"],
                    target_party_ledger=proposal["proposed_name"],
                    confidence=1.0,
                    source="approved_proposal",
                    approved_by=actor,
                )
            await record_activity(
                cur,
                tenant_id=tenant.id,
                action="master_proposal.approved" if approve else "master_proposal.rejected",
                actor_type="user",
                actor_id=actor,
                target_type="master_proposal",
                target_id=str(proposal_id),
                detail={"kind": proposal["kind"], "name": proposal["proposed_name"]},
            )
        return proposal

    async def sync_masters(
        self, tenant: TenantRef, *, agent_id: uuid.UUID | None, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Master data read back from Tally (Appendix C), so extraction and
        validation match the customer's existing masters."""
        stored: dict[str, int] = {}
        async with self.db.tenant(tenant) as cur:
            for kind in ("ledgers", "parties", "stock_items", "periods", "companies", "cost_centres", "godowns"):
                items = payload.get(kind)
                if not items:
                    continue
                await posting_repo.save_master_snapshot(
                    cur,
                    kind=kind,
                    payload=items,
                    company=payload.get("company"),
                    agent_id=agent_id,
                )
                stored[kind] = len(items)
            if payload.get("tax_ledgers"):
                await tenant_repo.upsert_ledger_mapping(
                    cur,
                    gstin=None,
                    name="__tenant_defaults__",
                    cgst_ledger=payload["tax_ledgers"].get("cgst"),
                    sgst_ledger=payload["tax_ledgers"].get("sgst"),
                    igst_ledger=payload["tax_ledgers"].get("igst"),
                    roundoff_ledger=payload["tax_ledgers"].get("roundoff"),
                    tds_ledger=payload["tax_ledgers"].get("tds"),
                    confidence=1.0,
                    source="tally_sync",
                )
                stored["tax_ledgers"] = len(payload["tax_ledgers"])
            await record_activity(
                cur,
                tenant_id=tenant.id,
                action="agent.master_sync",
                actor_type="agent",
                actor_id=str(agent_id) if agent_id else None,
                target_type="tenant",
                target_id=str(tenant.id),
                detail={"stored": stored, "company": payload.get("company")},
            )
        return {"stored": stored}

    # ── autonomy (section 12a) ──────────────────────────────────────────────

    async def autonomy_state(self, tenant: TenantRef) -> dict[str, Any]:
        async with self.db.tenant(tenant) as cur:
            metrics = await review_repo.metrics(cur)
            flags = await review_repo.flag_reason_counts(cur)
            vendors = await review_repo.accuracy_by_vendor(cur)
        async with self.db.control() as cur:
            await cur.execute(
                "select max(created_at) as last_change from public.autonomy_events where tenant_id = %s",
                (tenant.id,),
            )
            row = await cur.fetchone()
        if row and row["last_change"]:
            metrics.observation_days = max(0, (dt.datetime.now(dt.timezone.utc) - row["last_change"]).days)
        pack = self.pack_for(tenant)
        metrics.evaluation_cases = len(pack.evaluation.cases)
        metrics.evaluation_is_real = 1 if pack.evaluation.source == "real" else 0
        gate = evaluate_gate(tenant.autonomy_stage, metrics)
        demote, demote_reason = should_auto_demote(
            error_limit=tenant.error_rate_limit,
            metrics=metrics,
            auto_demote_enabled=tenant.auto_demote_on_breach,
        )
        return {
            "stage": describe_stage(tenant.autonomy_stage),
            "posting_mode": tenant.effective_posting_mode,
            "configured_mode": tenant.posting_mode,
            "kill_switch": tenant.kill_switch,
            "gate": gate.summary(),
            "evaluation": {
                "cases": len(pack.evaluation.cases),
                "source": pack.evaluation.source,
                "caveat": pack.evaluation.source_caveat,
            },
            "should_demote": demote,
            "demote_reason": demote_reason,
            "metrics": metrics.__dict__,
            "error_rate": float(metrics.error_rate),
            "top_flags": flags,
            "vendors": vendors,
            # Section 12a and Appendix C leave these open, so the admin screen
            # shows the live values rather than hard-coding them.
            "limits": {
                "confidence_threshold": tenant.confidence_threshold,
                "autopost_value_cap": float(tenant.autopost_value_cap),
                "daily_autopost_cap": tenant.daily_autopost_cap,
                "error_rate_limit": float(tenant.error_rate_limit),
                "auto_demote_on_breach": tenant.auto_demote_on_breach,
            },
        }

    async def set_limits(
        self,
        tenant: TenantRef,
        *,
        actor: str,
        confidence_threshold: float | None = None,
        autopost_value_cap: Decimal | None = None,
        daily_autopost_cap: int | None = None,
        error_rate_limit: Decimal | None = None,
        auto_demote_on_breach: bool | None = None,
    ) -> dict[str, Any]:
        """Every gate number in section 12a is a setting, not a constant."""
        updates: dict[str, Any] = {}
        if confidence_threshold is not None:
            if not 0.5 <= confidence_threshold <= 1:
                raise TenantScopeError("confidence threshold must be between 0.5 and 1")
            updates["confidence_threshold"] = confidence_threshold
        if autopost_value_cap is not None:
            if autopost_value_cap < 0:
                raise TenantScopeError("the auto-post value cap cannot be negative")
            updates["autopost_value_cap"] = autopost_value_cap
        if daily_autopost_cap is not None:
            if daily_autopost_cap < 0:
                raise TenantScopeError("the daily cap cannot be negative")
            updates["daily_autopost_cap"] = daily_autopost_cap
        if error_rate_limit is not None:
            if not Decimal("0") <= error_rate_limit <= Decimal("0.5"):
                raise TenantScopeError("the error rate limit must be between 0 and 0.5")
            updates["error_rate_limit"] = error_rate_limit
        if auto_demote_on_breach is not None:
            updates["auto_demote_on_breach"] = auto_demote_on_breach
        if not updates:
            raise TenantScopeError("no setting to change")

        assignments = ", ".join(f"{column} = %s" for column in updates)
        async with self.db.control() as cur:
            await cur.execute(
                f"update public.tenants set {assignments}, updated_at = now() "
                "where id = %s returning confidence_threshold, autopost_value_cap, "
                "daily_autopost_cap, error_rate_limit, auto_demote_on_breach",
                (*updates.values(), tenant.id),
            )
            updated = await cur.fetchone()
        await self._audit(
            tenant, actor, "autonomy.limits_set", {k: str(v) for k, v in updates.items()}
        )
        return {
            "confidence_threshold": float(updated["confidence_threshold"]),
            "autopost_value_cap": float(updated["autopost_value_cap"]),
            "daily_autopost_cap": updated["daily_autopost_cap"],
            "error_rate_limit": float(updated["error_rate_limit"]),
            "auto_demote_on_breach": bool(updated["auto_demote_on_breach"]),
        }

    async def set_posting_mode(
        self, tenant: TenantRef, *, mode: str, actor: str, reason: str | None = None
    ) -> dict[str, Any]:
        if mode not in {"shadow", "draft", "autonomous"}:
            raise TenantScopeError(f"unknown posting mode {mode!r}")
        async with self.db.control() as cur:
            await cur.execute(
                "update public.tenants set posting_mode = %s, kill_switch = %s where id = %s "
                "returning posting_mode, autonomy_stage, kill_switch",
                (mode, mode != "autonomous", tenant.id),
            )
            updated = await cur.fetchone()
            await cur.execute(
                """
                insert into public.autonomy_events
                  (tenant_id, from_mode, to_mode, from_stage, to_stage, reason, actor)
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant.id, tenant.posting_mode, mode, tenant.autonomy_stage,
                    tenant.autonomy_stage, reason or "set from the admin screen", actor,
                ),
            )
        await self._audit(tenant, actor, "autonomy.posting_mode_set", {"mode": mode, "reason": reason})
        return {"posting_mode": updated["posting_mode"], "kill_switch": updated["kill_switch"]}

    async def set_autonomy_stage(
        self, tenant: TenantRef, *, stage: int, actor: str, reason: str | None = None
    ) -> dict[str, Any]:
        if not 0 <= stage <= 4:
            raise TenantScopeError("autonomy stage must be between 0 and 4")
        async with self.db.control() as cur:
            await cur.execute(
                "select max(created_at) as last_change from public.autonomy_events where tenant_id = %s",
                (tenant.id,),
            )
            row = await cur.fetchone()
        async with self.db.tenant(tenant) as cur:
            metrics = await review_repo.metrics(cur)
        gate = evaluate_gate(tenant.autonomy_stage, metrics)
        if stage > tenant.autonomy_stage and not gate.passed and not reason:
            raise TenantScopeError(
                "the gate for this stage has not been passed; record a reason to override: "
                + "; ".join(check.detail for check in gate.blocking)
            )
        async with self.db.control() as cur:
            await cur.execute(
                "update public.tenants set autonomy_stage = %s, "
                "posting_mode = case when %s >= 2 and posting_mode <> 'shadow' then posting_mode "
                "                     when %s = 1 then 'shadow' else posting_mode end "
                "where id = %s returning posting_mode, autonomy_stage",
                (stage, stage, stage, tenant.id),
            )
            updated = await cur.fetchone()
            await cur.execute(
                """
                insert into public.autonomy_events
                  (tenant_id, from_mode, to_mode, from_stage, to_stage, reason, actor, metrics)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant.id, tenant.posting_mode, updated["posting_mode"],
                    tenant.autonomy_stage, stage, reason, actor, J(metrics.__dict__),
                ),
            )
        await self._audit(
            tenant, actor, "autonomy.stage_set", {"stage": stage, "reason": reason, "gate": gate.summary()}
        )
        return {"autonomy_stage": updated["autonomy_stage"], "posting_mode": updated["posting_mode"], "gate": gate.summary()}

    async def kill_switch(
        self, tenant: TenantRef, *, engaged: bool, actor: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Moves the tenant to draft mode at once (section 12a fallback)."""
        async with self.db.control() as cur:
            await cur.execute(
                "update public.tenants set kill_switch = %s, "
                "posting_mode = case when %s then 'draft' else posting_mode end "
                "where id = %s returning posting_mode, kill_switch",
                (engaged, engaged, tenant.id),
            )
            updated = await cur.fetchone()
            await cur.execute(
                """
                insert into public.autonomy_events (tenant_id, from_mode, to_mode, reason, actor)
                values (%s, %s, %s, %s, %s)
                """,
                (
                    tenant.id, tenant.posting_mode, updated["posting_mode"],
                    reason or ("kill switch engaged" if engaged else "kill switch released"), actor,
                ),
            )
        await self._audit(tenant, actor, "autonomy.kill_switch", {"engaged": engaged, "reason": reason})
        return {"posting_mode": updated["posting_mode"], "kill_switch": updated["kill_switch"]}

    async def set_rule_setting(
        self,
        tenant: TenantRef,
        *,
        rule_key: str,
        doc_type: str | None,
        enabled: bool,
        severity_override: str | None,
        settings: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        pack = self.pack_for(tenant)
        if rule_key not in {rule.key for rule in pack.rules}:
            raise TenantScopeError(
                f"{rule_key} is not a rule in pack {pack.key} {pack.version}"
            )
        async with self.db.tenant(tenant) as cur:
            row = await review_repo.upsert_rule_setting(
                cur,
                rule_key=rule_key,
                doc_type=doc_type,
                enabled=enabled,
                severity_override=severity_override,
                settings=settings,
                actor=actor,
            )
            await record_activity(
                cur,
                tenant_id=tenant.id,
                action="settings.rule_updated",
                actor_type="user",
                actor_id=actor,
                target_type="rule",
                target_id=rule_key,
                detail={
                    "enabled": enabled,
                    "severity_override": severity_override,
                    "settings": settings,
                },
            )
        return row

    async def rule_settings(self, tenant: TenantRef) -> list[dict[str, Any]]:
        pack = self.pack_for(tenant)
        async with self.db.tenant(tenant) as cur:
            stored = await review_repo.rule_settings(cur)
        return [
            {
                "key": rule.key,
                "title": rule.title,
                "description": rule.description,
                "applies_to": rule.applies_to,
                "severity": rule.severity,
                "requires_lookup": rule.requires_lookup,
                "pack_settings": rule.settings,
                "tenant": stored.get(
                    rule.key, {"enabled": True, "severity_override": None, "settings": {}}
                ),
            }
            for rule in pack.rules
        ]

    # ── dashboard and audit ─────────────────────────────────────────────────

    async def dashboard(self, tenant: TenantRef, *, days: int = 30) -> dict[str, Any]:
        async with self.db.tenant(tenant) as cur:
            metrics = await review_repo.metrics(cur, days=days)
            counts = await review_repo.queue_counts(cur)
            flags = await review_repo.flag_reason_counts(cur, days=days)
            vendors = await review_repo.accuracy_by_vendor(cur, days=days)
            await cur.execute(
                """
                select
                  count(*) as documents,
                  count(*) filter (where status = 'exception') as exceptions,
                  count(*) filter (where status = 'deduplicated') as duplicates,
                  count(*) filter (where status = 'in_review') as in_review,
                  count(*) filter (where status = 'posted') as posted
                from documents where received_at > now() - make_interval(days => %s)
                """,
                (days,),
            )
            documents = await cur.fetchone()
            await cur.execute(
                """
                select count(*) as n, coalesce(sum(amount_total), 0) as value
                  from vouchers where status = 'queued'
                """
            )
            queued = await cur.fetchone()
        async with self.db.control() as cur:
            await cur.execute(
                "select count(*) as n from public.agent_commands c "
                "where c.tenant_id = %s and c.status = 'queued' and c.expires_at > now()",
                (tenant.id,),
            )
            open_commands = await cur.fetchone()
        return {
            "window_days": days,
            "documents": documents,
            "metrics": metrics.__dict__,
            "error_rate": float(metrics.error_rate),
            "queue": counts,
            "queued_vouchers": queued,
            "open_agent_commands": int(open_commands["n"] if open_commands else 0),
            "top_flags": flags,
            "vendors": vendors,
        }

    async def activity(
        self, tenant: TenantRef, *, target_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        async with self.db.control() as cur:
            if target_id:
                await cur.execute(
                    "select * from public.activity_log where tenant_id = %s and target_id = %s "
                    "order by created_at desc limit %s",
                    (tenant.id, target_id, limit),
                )
            else:
                await cur.execute(
                    "select * from public.activity_log where tenant_id = %s "
                    "order by created_at desc limit %s",
                    (tenant.id, limit),
                )
            return list(await cur.fetchall())

    async def record_detail(self, tenant: TenantRef, record_id: uuid.UUID) -> dict[str, Any] | None:
        async with self.db.tenant(tenant) as cur:
            await cur.execute("select * from extracted_records where id = %s", (record_id,))
            record = await cur.fetchone()
            if record is None:
                return None
            record.pop("embedding", None)
            await cur.execute(
                "select * from rule_results where record_id = %s order by outcome", (record_id,)
            )
            rules = list(await cur.fetchall())
            await cur.execute(
                "select * from field_values where record_id = %s order by path", (record_id,)
            )
            fields = list(await cur.fetchall())
            await cur.execute(
                "select * from vouchers where record_id = %s order by created_at desc", (record_id,)
            )
            vouchers = list(await cur.fetchall())
            await cur.execute(
                "select * from review_items where record_id = %s order by created_at desc",
                (record_id,),
            )
            items = list(await cur.fetchall())
            await cur.execute(
                "select * from documents where id = %s", (record["document_id"],)
            )
            document = await cur.fetchone()
        return {
            "record": record,
            "rules": rules,
            "fields": fields,
            "vouchers": vouchers,
            "review_items": items,
            "document": document,
        }

    async def _audit(self, tenant: TenantRef, actor: str, action: str, detail: dict[str, Any]) -> None:
        async with self.db.control() as cur:
            await record_activity(
                cur,
                tenant_id=tenant.id,
                action=action,
                actor_type="user",
                actor_id=actor,
                detail=detail,
            )


# ── helpers ─────────────────────────────────────────────────────────────────


def _record_from_row(row: dict[str, Any]):
    from engine.models import ExtractedRecord

    payload = dict(row.get("payload") or {})
    return ExtractedRecord(
        doc_type=row["doc_type"],
        pack_key=row.get("pack_key") or "manufacturing",
        pack_version=row.get("pack_version") or "1.0.0",
        payload=payload,
        fields=[],
        page_from=row.get("page_from"),
        page_to=row.get("page_to"),
        order_index=row.get("order_index") or 0,
        model=payload.get("_extraction", {}).get("model"),
        schema_ok=True,
    )


def _draft_from_voucher(voucher: dict[str, Any]):
    from engine.models import VoucherDraft

    payload = voucher.get("payload") or {}
    try:
        return VoucherDraft.model_validate(payload)
    except Exception:
        return VoucherDraft(
            voucher_type=voucher["voucher_type"],
            voucher_date=voucher.get("voucher_date"),
            party_ledger=voucher.get("party_ledger"),
            narration=voucher.get("narration"),
            amount_total=Decimal(str(voucher.get("amount_total") or 0)),
            idempotency_key=voucher["idempotency_key"],
        )


def _lookup_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (IndexError, ValueError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _set_path(payload: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current: Any = payload
    for part in parts[:-1]:
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current.setdefault(part, {})
    last = parts[-1]
    if isinstance(current, list):
        current[int(last)] = value
    elif isinstance(current, dict):
        current[last] = value


def _rule_summary(summary) -> dict[str, Any]:
    return {
        "failed": summary.failed_keys,
        "warned": summary.warned_keys,
        "degraded": [r.rule_key for r in summary.degraded],
    }


def _clarification_template(template_key: str, document: dict[str, Any]) -> str:
    templates = {
        "clearer_copy": (
            "Hello, we received your document but could not read it clearly enough to "
            "record it. Please send a clearer photo or a digital copy, ideally the full "
            "page including the total and the tax lines."
        ),
        "missing_page": (
            "Hello, the document we received looks incomplete. Please send the remaining "
            "page(s), including the totals and the tax breakdown."
        ),
        "missing_details": (
            "Hello, some details are missing from the document we received (for example the "
            "invoice number, the GSTIN, or the tax split). Please confirm and resend."
        ),
    }
    base = templates.get(template_key, templates["clearer_copy"])
    return f"{base}\n\nReference: {document.get('file_name') or document.get('id')}"
