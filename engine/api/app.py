"""The engine's HTTP API.

The web app, calling Ygudy products and the bridge agent all talk to this service;
nothing else touches a tenant's schema (section 9).
"""

from __future__ import annotations

import contextlib
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from engine import __version__
from engine.api.deps import Principal, get_db, request_principal
from engine.db import Database, J, record_activity
from engine.operations import EngineOperations, TenantScopeError
from engine.packs.loader import sync_pack
from engine.pipeline import Pipeline
from engine.posting.commands import expire_stale_commands, pending_commands, record_command_result
from engine.posting.commands import verify_signature
from engine.settings import get_settings


class ApproveRequest(BaseModel):
    note: str | None = None
    edits: dict[str, str] = Field(default_factory=dict)
    one_off_fields: list[str] = Field(default_factory=list)


class RejectRequest(BaseModel):
    reason: str
    note: str | None = None


class HoldRequest(BaseModel):
    note: str


class RequestDocumentRequest(BaseModel):
    channel: str = "email"
    template_key: str = "clearer_copy"
    message: str | None = None


class ReassignRequest(BaseModel):
    assignee: str
    note: str | None = None


class BulkApproveRequest(BaseModel):
    item_ids: list[uuid.UUID]
    note: str | None = None
    group_by: str | None = None


class ReverseRequest(BaseModel):
    reason: str


class PostingModeRequest(BaseModel):
    mode: str
    reason: str | None = None


class StageRequest(BaseModel):
    stage: int
    reason: str | None = None


class KillSwitchRequest(BaseModel):
    engaged: bool
    reason: str | None = None


class LimitsRequest(BaseModel):
    confidence_threshold: float | None = None
    autopost_value_cap: float | None = None
    daily_autopost_cap: int | None = None
    error_rate_limit: float | None = None
    auto_demote_on_breach: bool | None = None


class RuleSettingRequest(BaseModel):
    rule_key: str
    doc_type: str | None = None
    enabled: bool = True
    severity_override: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


class AgentHeartbeat(BaseModel):
    tally_running: bool | None = None
    company_open: str | None = None
    period_open: bool | None = None
    queue_depth: int | None = None
    last_post_at: str | None = None
    app_version: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentCommandResult(BaseModel):
    status: str
    result: dict[str, Any] = Field(default_factory=dict)
    error_text: str | None = None
    signature: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class MasterSyncRequest(BaseModel):
    company: str | None = None
    ledgers: list[dict[str, Any]] = Field(default_factory=list)
    parties: list[dict[str, Any]] = Field(default_factory=list)
    stock_items: list[dict[str, Any]] = Field(default_factory=list)
    tax_ledgers: dict[str, Any] = Field(default_factory=dict)
    periods: list[dict[str, Any]] = Field(default_factory=list)
    companies: list[dict[str, Any]] = Field(default_factory=list)
    cost_centres: list[dict[str, Any]] = Field(default_factory=list)
    godowns: list[dict[str, Any]] = Field(default_factory=list)


class DocumentIntake(BaseModel):
    text: str
    file_name: str | None = None
    channel: str = "api"
    source_ref: str | None = None
    sender: str | None = None
    mime_type: str = "text/plain"
    pack_key: str | None = None
    prefer_model: bool = False


def create_app() -> FastAPI:
    app = FastAPI(
        title="Autonomous Data Entry Platform — engine",
        version=__version__,
        description=(
            "Intake, extraction, validation, confidence routing and posting. "
            "Driven by versioned Industry Packs."
        ),
    )
    settings = get_settings()
    app.state.settings = settings
    app.state.db = Database(settings)

    @app.on_event("startup")
    async def _startup() -> None:
        await app.state.db.open()
        registry = app.state.registry = __import__(
            "engine.packs.loader", fromlist=["PackRegistry"]
        ).PackRegistry()
        app.state.ops = EngineOperations(app.state.db, registry, Pipeline(app.state.db, registry))

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await app.state.db.close()

    router = APIRouter(prefix="/v1")

    # ── health and packs ────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> dict[str, Any]:
        db: Database = app.state.db
        try:
            async with db.control() as cur:
                await cur.execute("select 1")
            database = "ok"
        except Exception as exc:  # pragma: no cover - readiness path
            database = f"error: {type(exc).__name__}"
        registry = getattr(app.state, "registry", None)
        return {
            "status": "ok" if database == "ok" else "degraded",
            "version": __version__,
            "environment": settings.engine_env,
            "database": database,
            "packs": registry.available() if registry else [],
            "llm_enabled": settings.llm_enabled,
            "gstn_source": "fixture (prototype)" if settings.gsp_use_fixture else "gsp",
            "langfuse_enabled": settings.langfuse_enabled,
        }

    @router.get("/packs")
    async def list_packs(principal: Principal = Depends(request_principal)) -> dict[str, Any]:
        ops: EngineOperations = app.state.ops
        pack = ops.pack_for(principal.tenant)
        return {
            "available": ops.registry.available(),
            "active": {
                "key": pack.key,
                "name": pack.manifest.name,
                "version": pack.version,
                "status": pack.manifest.status,
                "checksum": pack.checksum,
                "documents": [
                    {
                        "key": spec.key,
                        "label": spec.label,
                        "mandatory": spec.mandatory_fields,
                        "posts_to": spec.posts_to,
                    }
                    for spec in pack.documents.values()
                ],
                "rules": [
                    {
                        "key": rule.key,
                        "title": rule.title,
                        "version": rule.version,
                        "severity": rule.severity,
                        "applies_to": rule.applies_to,
                        "requires_lookup": rule.requires_lookup,
                    }
                    for rule in pack.rules
                ],
                "targets": sorted(pack.connectors.targets),
                "evaluation_cases": len(pack.evaluation.cases),
            },
        }

    @router.post("/packs/sync")
    async def sync_packs(principal: Principal = Depends(request_principal)) -> dict[str, Any]:
        principal.require_role("owner")
        ops: EngineOperations = app.state.ops
        db: Database = app.state.db
        synced = []
        for key in ops.registry.available():
            pack = ops.registry.get(key)
            await sync_pack(db, pack, status=pack.manifest.status)
            synced.append({"key": key, "version": pack.version, "checksum": pack.checksum})
        return {"synced": synced}

    # ── documents ───────────────────────────────────────────────────────────

    @router.post("/documents/upload")
    async def upload_document(
        file: UploadFile = File(...),
        channel: str = Form("web_upload"),
        sender: str | None = Form(None),
        prefer_model: bool = Form(False),
        principal: Principal = Depends(request_principal),
    ) -> dict[str, Any]:
        if principal.kind == "agent":
            raise HTTPException(status_code=403, detail="agents do not upload documents")
        ops: EngineOperations = app.state.ops
        content = await file.read()
        if len(content) > 25 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="files above 25 MB are not accepted")
        result = await ops.pipeline.ingest(
            principal.tenant,
            content=content,
            file_name=file.filename,
            mime_type=file.content_type,
            channel=channel,
            sender=sender,
            prefer_model=prefer_model,
        )
        return _ingest_payload(result)

    @router.post("/documents")
    async def intake_text(
        body: DocumentIntake, principal: Principal = Depends(request_principal)
    ) -> dict[str, Any]:
        """The API and webhook intake used by calling Ygudy products (section 8)."""
        ops: EngineOperations = app.state.ops
        result = await ops.pipeline.ingest(
            principal.tenant,
            text=body.text,
            file_name=body.file_name,
            mime_type=body.mime_type or "text/plain",
            channel=body.channel,
            source_ref=body.source_ref,
            sender=body.sender,
            prefer_model=body.prefer_model,
        )
        return _ingest_payload(result)

    # ── review queue ────────────────────────────────────────────────────────

    @router.get("/review/queue")
    async def review_queue(
        status_filter: str = Query("pending,held,clarification_requested", alias="status"),
        doc_type: str | None = None,
        vendor: str | None = None,
        reason: str | None = None,
        assignee: str | None = None,
        band: str | None = None,
        min_amount: float | None = None,
        limit: int = Query(50, le=200),
        offset: int = 0,
        principal: Principal = Depends(request_principal),
    ) -> dict[str, Any]:
        ops: EngineOperations = app.state.ops
        return await ops.queue(
            principal.tenant,
            statuses=tuple(s for s in status_filter.split(",") if s),
            doc_type=doc_type,
            vendor=vendor,
            reason=reason,
            assignee=assignee,
            band=band,
            min_amount=min_amount,
            limit=limit,
            offset=offset,
        )

    @router.get("/review/items/{item_id}")
    async def review_item(
        item_id: uuid.UUID, principal: Principal = Depends(request_principal)
    ) -> dict[str, Any]:
        ops: EngineOperations = app.state.ops
        detail = await ops.item(principal.tenant, item_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="review item not found")
        return detail

    @router.post("/review/items/{item_id}/approve")
    async def approve_item(
        item_id: uuid.UUID,
        body: ApproveRequest,
        principal: Principal = Depends(request_principal),
    ) -> dict[str, Any]:
        principal.require_role("owner", "reviewer")
        ops: EngineOperations = app.state.ops
        return await _guard(
            ops.approve(
                principal.tenant,
                item_id,
                actor=principal.actor,
                actor_role=principal.role,
                note=body.note,
                edits=body.edits,
                one_off_fields=body.one_off_fields,
            )
        )

    @router.post("/review/items/{item_id}/reject")
    async def reject_item(
        item_id: uuid.UUID, body: RejectRequest, principal: Principal = Depends(request_principal)
    ) -> dict[str, Any]:
        principal.require_role("owner", "reviewer")
        ops: EngineOperations = app.state.ops
        return await _guard(
            ops.reject(
                principal.tenant,
                item_id,
                actor=principal.actor,
                actor_role=principal.role,
                reason=body.reason,
                note=body.note,
            )
        )

    @router.post("/review/items/{item_id}/hold")
    async def hold_item(
        item_id: uuid.UUID, body: HoldRequest, principal: Principal = Depends(request_principal)
    ) -> dict[str, Any]:
        principal.require_role("owner", "reviewer")
        ops: EngineOperations = app.state.ops
        return await _guard(
            ops.hold(
                principal.tenant,
                item_id,
                actor=principal.actor,
                actor_role=principal.role,
                note=body.note,
            )
        )

    @router.post("/review/items/{item_id}/request-document")
    async def request_document(
        item_id: uuid.UUID,
        body: RequestDocumentRequest,
        principal: Principal = Depends(request_principal),
    ) -> dict[str, Any]:
        principal.require_role("owner", "reviewer")
        ops: EngineOperations = app.state.ops
        return await _guard(
            ops.request_document(
                principal.tenant,
                item_id,
                actor=principal.actor,
                actor_role=principal.role,
                channel=body.channel,
                template_key=body.template_key,
                message=body.message,
            )
        )

    @router.post("/review/items/{item_id}/reassign")
    async def reassign_item(
        item_id: uuid.UUID,
        body: ReassignRequest,
        principal: Principal = Depends(request_principal),
    ) -> dict[str, Any]:
        principal.require_role("owner", "reviewer")
        ops: EngineOperations = app.state.ops
        return await _guard(
            ops.reassign(
                principal.tenant,
                item_id,
                actor=principal.actor,
                actor_role=principal.role,
                assignee=body.assignee,
                note=body.note,
            )
        )

    @router.post("/review/bulk-approve")
    async def bulk_approve(
        body: BulkApproveRequest, principal: Principal = Depends(request_principal)
    ) -> dict[str, Any]:
        principal.require_role("owner", "reviewer")
        ops: EngineOperations = app.state.ops
        return await _guard(
            ops.bulk_approve(
                principal.tenant,
                body.item_ids,
                actor=principal.actor,
                actor_role=principal.role,
                note=body.note,
                group_by=body.group_by,
            )
        )

    # ── exceptions ──────────────────────────────────────────────────────────

    @router.get("/exceptions")
    async def exceptions(
        limit: int = Query(50, le=200), principal: Principal = Depends(request_principal)
    ) -> dict[str, Any]:
        db: Database = app.state.db
        async with db.tenant(principal.tenant) as cur:
            await cur.execute(
                """
                select d.id, d.file_name, d.intake_channel, d.sender, d.doc_type,
                       d.status, d.exception_reason, d.received_at,
                       (select count(*) from extracted_records er where er.document_id = d.id)
                         as record_count
                  from documents d
                 where d.status = 'exception'
                 order by d.received_at desc
                 limit %s
                """,
                (limit,),
            )
            documents = list(await cur.fetchall())
        return {"count": len(documents), "documents": documents}

    @router.post("/exceptions/{document_id}/retry")
    async def retry_exception(
        document_id: uuid.UUID,
        prefer_model: bool = False,
        principal: Principal = Depends(request_principal),
    ) -> dict[str, Any]:
        principal.require_role("owner", "reviewer")
        db: Database = app.state.db
        ops: EngineOperations = app.state.ops
        async with db.tenant(principal.tenant) as cur:
            await cur.execute(
                "select file_name, mime_type, raw_meta from documents where id = %s",
                (document_id,),
            )
            document = await cur.fetchone()
            if document is None:
                raise HTTPException(status_code=404, detail="document not found")
            await cur.execute(
                "select meta, text_digest from document_pages where document_id = %s "
                "order by page_no limit 1",
                (document_id,),
            )
            page = await cur.fetchone()
            stored = (page or {}).get("meta", {}) if page else {}
            text = (page or {}).get("text_digest") if page else None
            await cur.execute(
                "update documents set status = 'received', exception_reason = null where id = %s",
                (document_id,),
            )
        text = stored.get("text") if stored else text
        if not text:
            raise HTTPException(
                status_code=409,
                detail=(
                    "the original file content is not retained locally, so this document "
                    "cannot be re-read; ask the sender for a new copy"
                ),
            )
        result = await ops.pipeline.ingest(
            principal.tenant,
            text=text,
            file_name=document["file_name"],
            mime_type=document["mime_type"],
            channel="web_upload",
            prefer_model=prefer_model,
            source_ref=str(document_id),
        )
        return _ingest_payload(result)

    @router.post("/exceptions/{document_id}/reject")
    async def reject_exception(
        document_id: uuid.UUID,
        reason: str,
        principal: Principal = Depends(request_principal),
    ) -> dict[str, Any]:
        """Appendix D: an exception the engine cannot process can be closed."""
        principal.require_role("owner", "reviewer")
        db: Database = app.state.db
        async with db.tenant(principal.tenant) as cur:
            await cur.execute(
                """
                update documents
                   set status = 'rejected', exception_reason = %s, processed_at = now()
                 where id = %s and status = 'exception'
                 returning id, file_name
                """,
                (f"rejected by reviewer: {reason}", document_id),
            )
            updated = await cur.fetchone()
            if updated is None:
                raise HTTPException(status_code=404, detail="no open exception with that id")
            await record_activity(
                cur,
                tenant_id=principal.tenant.id,
                action="document.exception_rejected",
                actor_type="user",
                actor_id=principal.actor,
                target_type="document",
                target_id=str(document_id),
                detail={"reason": reason},
                severity="info",
            )
        return {"document_id": str(document_id), "status": "rejected"}

    # ── vouchers ────────────────────────────────────────────────────────────

    @router.get("/vouchers/{voucher_id}")
    async def get_voucher(
        voucher_id: uuid.UUID, principal: Principal = Depends(request_principal)
    ) -> dict[str, Any]:
        ops: EngineOperations = app.state.ops
        voucher = await ops.voucher(principal.tenant, voucher_id)
        if voucher is None:
            raise HTTPException(status_code=404, detail="voucher not found")
        return voucher

    @router.post("/vouchers/{voucher_id}/reverse")
    async def reverse_voucher(
        voucher_id: uuid.UUID,
        body: ReverseRequest,
        principal: Principal = Depends(request_principal),
    ) -> dict[str, Any]:
        principal.require_role("owner")
        ops: EngineOperations = app.state.ops
        return await _guard(
            ops.reverse_voucher(
                principal.tenant, voucher_id, actor=principal.actor, reason=body.reason
            )
        )

    @router.post("/vouchers/export")
    async def export_vouchers(
        body: dict[str, Any], principal: Principal = Depends(request_principal)
    ) -> PlainTextResponse:
        """Mode B: a Tally-compatible XML or CSV import file (Appendix C)."""
        ops: EngineOperations = app.state.ops
        fmt = str(body.get("format", "csv"))
        ids = [uuid.UUID(str(v)) for v in body.get("voucher_ids", [])]
        if not ids:
            raise HTTPException(status_code=400, detail="voucher_ids is required")
        export = await ops.export(principal.tenant, ids, fmt=fmt)
        media = "text/csv" if export["format"] == "csv" else "application/xml"
        return PlainTextResponse(export["content"], media_type=media)

    # ── master data ─────────────────────────────────────────────────────────

    @router.get("/master/proposals")
    async def master_proposals(principal: Principal = Depends(request_principal)) -> dict[str, Any]:
        ops: EngineOperations = app.state.ops
        return {"proposals": await ops.master_proposals(principal.tenant)}

    @router.post("/master/proposals/{proposal_id}/decide")
    async def decide_proposal(
        proposal_id: uuid.UUID,
        approve: bool,
        principal: Principal = Depends(request_principal),
    ) -> dict[str, Any]:
        principal.require_role("owner", "reviewer")
        ops: EngineOperations = app.state.ops
        return await _guard(
            ops.decide_master_proposal(
                principal.tenant, proposal_id, approve=approve, actor=principal.actor
            )
        )

    # ── learning loop ───────────────────────────────────────────────────────

    @router.get("/learning/suggestions")
    async def learning_suggestions(
        principal: Principal = Depends(request_principal),
    ) -> dict[str, Any]:
        from engine.repo import review as review_repo

        db: Database = app.state.db
        async with db.tenant(principal.tenant) as cur:
            suggestions = await review_repo.list_rule_suggestions(cur)
        return {"suggestions": suggestions}

    @router.post("/learning/suggestions/{suggestion_id}/decide")
    async def decide_suggestion(
        suggestion_id: uuid.UUID,
        accept: bool,
        principal: Principal = Depends(request_principal),
    ) -> dict[str, Any]:
        principal.require_role("owner")
        from engine.repo import review as review_repo

        db: Database = app.state.db
        async with db.tenant(principal.tenant) as cur:
            suggestion = await review_repo.decide_rule_suggestion(
                cur, suggestion_id, accept=accept, actor=principal.actor
            )
            if suggestion is None:
                raise HTTPException(status_code=404, detail="suggestion not found")
            if accept and suggestion["kind"] == "ledger_mapping":
                proposal = suggestion["proposal"] or {}
                for field in ("target_party_ledger", "purchase_ledger", "expense_ledger"):
                    if field in proposal:
                        await __import__(
                            "engine.repo.tenant", fromlist=["upsert_ledger_mapping"]
                        ).upsert_ledger_mapping(
                            cur,
                            gstin=suggestion["vendor_gstin"],
                            name=None,
                            confidence=1.0,
                            source="learned",
                            approved_by=principal.actor,
                            **{field: proposal[field]},
                        )
        return suggestion

    # ── autonomy and settings ───────────────────────────────────────────────

    @router.get("/autonomy")
    async def autonomy(principal: Principal = Depends(request_principal)) -> dict[str, Any]:
        ops: EngineOperations = app.state.ops
        return await ops.autonomy_state(principal.tenant)

    @router.post("/autonomy/stage")
    async def set_stage(
        body: StageRequest, principal: Principal = Depends(request_principal)
    ) -> dict[str, Any]:
        principal.require_role("owner")
        ops: EngineOperations = app.state.ops
        return await _guard(
            ops.set_autonomy_stage(
                principal.tenant, stage=body.stage, actor=principal.actor, reason=body.reason
            )
        )

    @router.post("/autonomy/posting-mode")
    async def set_posting_mode(
        body: PostingModeRequest, principal: Principal = Depends(request_principal)
    ) -> dict[str, Any]:
        principal.require_role("owner")
        ops: EngineOperations = app.state.ops
        return await _guard(
            ops.set_posting_mode(
                principal.tenant, mode=body.mode, actor=principal.actor, reason=body.reason
            )
        )

    @router.post("/autonomy/kill-switch")
    async def kill_switch(
        body: KillSwitchRequest, principal: Principal = Depends(request_principal)
    ) -> dict[str, Any]:
        principal.require_role("owner")
        ops: EngineOperations = app.state.ops
        return await _guard(
            ops.kill_switch(
                principal.tenant, engaged=body.engaged, actor=principal.actor, reason=body.reason
            )
        )

    @router.post("/settings/limits")
    async def set_limits(
        body: LimitsRequest, principal: Principal = Depends(request_principal)
    ) -> dict[str, Any]:
        """The autonomy gate numbers and safety caps, per tenant (section 12a, Appendix C)."""
        principal.require_role("owner")
        ops: EngineOperations = app.state.ops
        return await _guard(
            ops.set_limits(
                principal.tenant,
                actor=principal.actor,
                confidence_threshold=body.confidence_threshold,
                autopost_value_cap=(
                    Decimal(str(body.autopost_value_cap))
                    if body.autopost_value_cap is not None
                    else None
                ),
                daily_autopost_cap=body.daily_autopost_cap,
                error_rate_limit=(
                    Decimal(str(body.error_rate_limit))
                    if body.error_rate_limit is not None
                    else None
                ),
                auto_demote_on_breach=body.auto_demote_on_breach,
            )
        )

    @router.get("/settings/rules")
    async def rules(principal: Principal = Depends(request_principal)) -> dict[str, Any]:
        ops: EngineOperations = app.state.ops
        return {"rules": await ops.rule_settings(principal.tenant)}

    @router.post("/settings/rules")
    async def update_rule(
        body: RuleSettingRequest, principal: Principal = Depends(request_principal)
    ) -> dict[str, Any]:
        principal.require_role("owner")
        ops: EngineOperations = app.state.ops
        return await _guard(
            ops.set_rule_setting(
                principal.tenant,
                rule_key=body.rule_key,
                doc_type=body.doc_type,
                enabled=body.enabled,
                severity_override=body.severity_override,
                settings=body.settings,
                actor=principal.actor,
            )
        )

    # ── dashboard and audit ─────────────────────────────────────────────────

    @router.get("/dashboard")
    async def dashboard(
        days: int = Query(30, ge=1, le=365), principal: Principal = Depends(request_principal)
    ) -> dict[str, Any]:
        ops: EngineOperations = app.state.ops
        return await ops.dashboard(principal.tenant, days=days)

    @router.get("/activity")
    async def activity(
        target_id: str | None = None,
        limit: int = Query(100, le=500),
        principal: Principal = Depends(request_principal),
    ) -> dict[str, Any]:
        ops: EngineOperations = app.state.ops
        return {
            "entries": await ops.activity(principal.tenant, target_id=target_id, limit=limit)
        }

    @router.get("/records/{record_id}")
    async def record_detail(
        record_id: uuid.UUID, principal: Principal = Depends(request_principal)
    ) -> dict[str, Any]:
        ops: EngineOperations = app.state.ops
        detail = await ops.record_detail(principal.tenant, record_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="record not found")
        return detail

    # ── bridge agent (Appendix E) ───────────────────────────────────────────

    @router.post("/agent/hello")
    async def agent_hello(principal: Principal = Depends(request_principal)) -> dict[str, Any]:
        if principal.kind != "agent":
            raise HTTPException(status_code=403, detail="this endpoint is for bridge agents")
        db: Database = app.state.db
        async with db.control() as cur:
            await cur.execute(
                "update public.agents set last_heartbeat_at = now() where id = %s",
                (principal.agent_id,),
            )
        return {
            "tenant": principal.tenant.slug,
            "pack_key": principal.tenant.pack_key,
            "posting_mode": principal.tenant.effective_posting_mode,
            "autonomy_stage": principal.tenant.autonomy_stage,
        }

    @router.post("/agent/heartbeat")
    async def agent_heartbeat(
        body: AgentHeartbeat, principal: Principal = Depends(request_principal)
    ) -> dict[str, Any]:
        if principal.kind != "agent":
            raise HTTPException(status_code=403, detail="this endpoint is for bridge agents")
        db: Database = app.state.db
        async with db.control() as cur:
            await cur.execute(
                """
                insert into public.agent_heartbeats
                  (agent_id, tenant_id, tally_running, company_open, period_open, queue_depth,
                   last_post_at, app_version, payload)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    principal.agent_id,
                    principal.tenant.id,
                    body.tally_running,
                    body.company_open,
                    body.period_open,
                    body.queue_depth,
                    body.last_post_at,
                    body.app_version,
                    J(body.payload),
                ),
            )
            await cur.execute(
                "update public.agents set last_heartbeat_at = now(), "
                "app_version = coalesce(%s, app_version), "
                "tally_company = coalesce(%s, tally_company) where id = %s",
                (body.app_version, body.company_open, principal.agent_id),
            )
            expired = await expire_stale_commands(cur, principal.tenant.id)
        return {"ok": True, "expired_commands": expired, "posting_mode": principal.tenant.effective_posting_mode}

    @router.get("/agent/commands")
    async def agent_commands(
        limit: int = Query(20, le=100), principal: Principal = Depends(request_principal)
    ) -> dict[str, Any]:
        if principal.kind != "agent":
            raise HTTPException(status_code=403, detail="this endpoint is for bridge agents")
        db: Database = app.state.db
        async with db.control() as cur:
            commands = await pending_commands(
                cur, principal.tenant.id, agent_id=principal.agent_id, limit=limit
            )
            if commands:
                await cur.execute(
                    "update public.agent_commands set status = 'sent', sent_at = now() "
                    "where id = any(%s) and status = 'queued'",
                    ([c["id"] for c in commands],),
                )
        return {"commands": commands}

    @router.post("/agent/commands/{command_id}/result")
    async def agent_command_result(
        command_id: uuid.UUID,
        body: AgentCommandResult,
        principal: Principal = Depends(request_principal),
    ) -> dict[str, Any]:
        if principal.kind != "agent":
            raise HTTPException(status_code=403, detail="this endpoint is for bridge agents")
        db: Database = app.state.db
        async with db.control() as cur:
            await cur.execute(
                "select * from public.agent_commands where id = %s and tenant_id = %s",
                (command_id, principal.tenant.id),
            )
            command = await cur.fetchone()
            if command is None:
                raise HTTPException(status_code=404, detail="command not found for this agent")
            if body.payload and body.signature:
                if not verify_signature(body.payload, body.signature):
                    raise HTTPException(status_code=400, detail="the result signature is not valid")
            await record_command_result(
                cur,
                command_id,
                status=body.status,
                result=body.result,
                error_text=body.error_text,
            )
        await _apply_command_result(app, principal, command, body)
        return {"ok": True}

    @router.post("/agent/masters")
    async def agent_masters(
        body: MasterSyncRequest, principal: Principal = Depends(request_principal)
    ) -> dict[str, Any]:
        if principal.kind != "agent":
            raise HTTPException(status_code=403, detail="this endpoint is for bridge agents")
        ops: EngineOperations = app.state.ops
        return await ops.sync_masters(
            principal.tenant, agent_id=principal.agent_id, payload=body.model_dump()
        )

    app.include_router(router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"service": "autonomous-data-entry-engine", "version": __version__}

    return app


async def _apply_command_result(
    app: FastAPI, principal: Principal, command: dict[str, Any], body: AgentCommandResult
) -> None:
    """Reflect a command's outcome on the voucher and record it in the audit trail."""
    db: Database = app.state.db
    outcome = body.result or {}
    idempotency_key = command["idempotency_key"]
    reversed_ = command["kind"] == "reverse_voucher"
    async with db.tenant(principal.tenant) as cur:
        if reversed_:
            await cur.execute(
                "select id from vouchers where idempotency_key like %s || '%%' "
                "order by created_at desc limit 1",
                (idempotency_key.rsplit(":", 1)[0],),
            )
        else:
            await cur.execute(
                "select id from vouchers where idempotency_key = %s", (idempotency_key,)
            )
        row = await cur.fetchone()
        if row is None:
            return
        voucher_id = row["id"]
        if body.status == "acked":
            await cur.execute(
                """
                update vouchers
                   set status = %s, tally_voucher_number = %s, tally_voucher_id = %s,
                       posted_at = case when %s then now() else posted_at end,
                       reversed_at = case when %s then now() else reversed_at end,
                       error_text = null
                 where id = %s
                """,
                (
                    "reversed" if reversed_ else "posted",
                    outcome.get("voucher_number"),
                    outcome.get("voucher_id"),
                    not reversed_,
                    reversed_,
                    voucher_id,
                ),
            )
            await cur.execute(
                "update extracted_records set status = %s where id = "
                "(select record_id from vouchers where id = %s)",
                ("reversed" if reversed_ else "posted"),
                voucher_id,
            )
        elif body.status == "failed":
            # Tally's error text becomes a plain-language reason in the queue.
            await cur.execute(
                """
                update vouchers set status = 'failed', error_text = %s where id = %s
                """,
                (body.error_text, voucher_id),
            )
            await cur.execute(
                """
                insert into review_items
                  (record_id, document_id, status, priority, priority_band, reasons, amount_total,
                   doc_type, vendor_name)
                select v.record_id, v.document_id, 'pending', 8, 'rule_failure', %s, v.amount_total,
                       er.doc_type, er.vendor_name
                  from vouchers v join extracted_records er on er.id = v.record_id
                 where v.id = %s
                """,
                (
                    J(
                        [
                            {
                                "code": "tally_rejected",
                                "message": body.error_text
                                or "Tally rejected this voucher; see the agent log.",
                                "fields": [],
                                "rule_key": None,
                            }
                        ]
                    ),
                    voucher_id,
                ),
            )
        await record_activity(
            cur,
            tenant_id=principal.tenant.id,
            action=f"agent.command_{body.status}",
            actor_type="agent",
            actor_id=principal.actor,
            target_type="voucher",
            target_id=str(voucher_id),
            detail={"kind": command["kind"], "result": outcome, "error": body.error_text},
            severity="info" if body.status == "acked" else "error",
        )


async def _guard(coro):
    try:
        return await coro
    except TenantScopeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _ingest_payload(result) -> dict[str, Any]:
    return {
        "document_id": str(result.document_id),
        "status": result.status,
        "doc_type": result.doc_type,
        "doc_type_confidence": result.doc_type_confidence,
        "segments": result.segments,
        "records": result.records,
        "review_item_ids": [str(i) for i in result.review_item_ids],
        "voucher_ids": [str(v) for v in result.voucher_ids],
        "posted_autonomously": result.posted_autonomously,
        "queued": result.queued,
        "needs_review": result.needs_review,
        "duplicate_of": str(result.duplicate_of) if result.duplicate_of else None,
        "warnings": result.warnings[:10],
    }


app = create_app()
