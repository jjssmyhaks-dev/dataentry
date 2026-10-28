"""Vouchers, posting attempts, master data and shadow comparisons."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import psycopg

from engine.db import J, TenantRef
from engine.models import ExtractedRecord, VoucherDraft


async def insert_voucher(
    cur: psycopg.Cursor,
    *,
    tenant: TenantRef,
    record_row: dict[str, Any],
    record: ExtractedRecord,
    draft: VoucherDraft,
    status: str,
) -> dict[str, Any]:
    await cur.execute(
        """
        insert into vouchers
          (record_id, document_id, voucher_type, voucher_date, party_ledger, narration, payload,
           amount_total, gst_cgst, gst_sgst, gst_igst, roundoff, place_of_supply, tally_company,
           idempotency_key, autonomy_stage, posting_mode, status, queued_at)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                case when %s = 'queued' then now() else null end)
        returning *
        """,
        (
            record_row["id"],
            record_row["document_id"],
            draft.voucher_type,
            draft.voucher_date,
            draft.party_ledger,
            draft.narration,
            J(draft.model_dump(mode="json")),
            draft.amount_total,
            draft.gst_cgst,
            draft.gst_sgst,
            draft.gst_igst,
            draft.roundoff,
            draft.place_of_supply,
            draft.tally_company,
            draft.idempotency_key,
            tenant.autonomy_stage,
            tenant.effective_posting_mode,
            status,
            status,
        ),
    )
    return await cur.fetchone()


async def find_voucher_by_idempotency(cur: psycopg.Cursor, key: str) -> dict[str, Any] | None:
    await cur.execute("select * from vouchers where idempotency_key = %s limit 1", (key,))
    return await cur.fetchone()


async def set_voucher_status(
    cur: psycopg.Cursor, voucher_id: uuid.UUID, status: str, **fields: Any
) -> None:
    payload = {**fields}
    payload.pop("status", None)
    assignments = ["status = %s"]
    params: list[Any] = [status]
    for name in ("tally_voucher_number", "tally_voucher_id", "error_text", "staged_at"):
        if name in payload:
            assignments.append(f"{name} = %s")
            params.append(payload.pop(name))
    if payload.pop("posted", False):
        assignments.append("posted_at = now()")
    if payload.pop("reversed", False):
        assignments.append("reversed_at = now()")
    if payload.pop("reversal_voucher_id", None) is not None:
        assignments.append("reversal_voucher_id = %s")
        params.append(fields["reversal_voucher_id"])
    if payload.pop("reverses_voucher_id", None) is not None:
        assignments.append("reverses_voucher_id = %s")
        params.append(fields["reverses_voucher_id"])
    params.append(voucher_id)
    await cur.execute(
        f"update vouchers set {', '.join(assignments)} where id = %s", tuple(params)
    )


async def set_record_status_for_voucher(
    cur: psycopg.Cursor, voucher_id: uuid.UUID, status: str
) -> None:
    await cur.execute(
        """
        update extracted_records set status = %s
         where id = (select record_id from vouchers where id = %s)
        """,
        (status, voucher_id),
    )


async def start_posting_attempt(
    cur: psycopg.Cursor,
    *,
    voucher_id: uuid.UUID,
    mode: str,
    connector: str,
    request: dict[str, Any] | None = None,
    command_id: uuid.UUID | None = None,
    attempt: int = 1,
) -> uuid.UUID:
    await cur.execute(
        """
        insert into posting_attempts (voucher_id, command_id, attempt, mode, connector, request)
        values (%s, %s, %s, %s, %s, %s)
        returning id
        """,
        (voucher_id, command_id, attempt, mode, connector, J(request or {})),
    )
    row = await cur.fetchone()
    return row["id"]


async def finish_posting_attempt(
    cur: psycopg.Cursor,
    attempt_id: uuid.UUID,
    *,
    outcome: str,
    response: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_text: str | None = None,
) -> None:
    await cur.execute(
        """
        update posting_attempts
           set outcome = %s, response = %s, error_code = %s, error_text = %s, finished_at = now()
         where id = %s
        """,
        (outcome, J(response or {}), error_code, error_text, attempt_id),
    )


async def link_compliance_to_voucher(
    cur: psycopg.Cursor, *, record_id: uuid.UUID, voucher_id: uuid.UUID
) -> None:
    """E-way bill and e-invoice are stored as compliance records linked to the
    voucher (Appendix C)."""
    await cur.execute(
        """
        update compliance_records set voucher_id = %s
         where record_id in (
           select id from extracted_records
            where document_id = (select document_id from extracted_records where id = %s)
         )
        """,
        (voucher_id, record_id),
    )


async def auto_posted_today(cur: psycopg.Cursor) -> int:
    await cur.execute(
        "select count(*) as n from vouchers where posted_at >= date_trunc('day', now())"
    )
    row = await cur.fetchone()
    return int(row["n"] if row else 0)


async def touch_ledger_mapping(cur: psycopg.Cursor, *, gstin: str | None, name: str | None) -> None:
    await cur.execute(
        """
        update ledger_mappings
           set use_count = use_count + 1, last_used_at = now()
         where (party_gstin = %s and %s is not null)
            or (party_name_norm = %s and %s is not null)
        """,
        (gstin, gstin, name, name),
    )


async def save_master_snapshot(
    cur: psycopg.Cursor,
    *,
    kind: str,
    payload: list[dict[str, Any]],
    company: str | None = None,
    agent_id: uuid.UUID | None = None,
) -> None:
    await cur.execute(
        """
        insert into master_snapshots (agent_id, kind, company, item_count, payload)
        values (%s, %s, %s, %s, %s)
        """,
        (agent_id, kind, company, len(payload), J(payload)),
    )


async def insert_master_proposal(
    cur: psycopg.Cursor,
    *,
    kind: str,
    proposed_name: str,
    reason: str | None = None,
    gstin: str | None = None,
    parent_group: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A new party or ledger is proposed for approval, never created silently."""
    await cur.execute(
        """
        insert into master_proposals (kind, proposed_name, parent_group, gstin, reason, evidence)
        values (%s, %s, %s, %s, %s, %s)
        returning *
        """,
        (kind, proposed_name, parent_group, gstin, reason, J(evidence or {})),
    )
    return await cur.fetchone()


async def pending_master_proposals(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    await cur.execute(
        "select * from master_proposals where status = 'pending' order by created_at limit 200"
    )
    return list(await cur.fetchall())


async def decide_master_proposal(
    cur: psycopg.Cursor, proposal_id: uuid.UUID, *, approve: bool, actor: str
) -> dict[str, Any] | None:
    await cur.execute(
        """
        update master_proposals
           set status = %s, decided_by = %s, decided_at = now()
         where id = %s
        returning *
        """,
        ("approved" if approve else "rejected", actor, proposal_id),
    )
    return await cur.fetchone()


async def insert_shadow_comparison(
    cur: psycopg.Cursor,
    *,
    record_id: uuid.UUID,
    voucher_id: uuid.UUID | None,
    human_payload: dict[str, Any],
    field_diffs: list[dict[str, Any]],
    match_score: Decimal | float | None,
    wrong_ledger: bool = False,
    wrong_tax_split: bool = False,
) -> dict[str, Any]:
    await cur.execute(
        """
        insert into shadow_comparisons
          (record_id, voucher_id, human_payload, field_diffs, match_score, wrong_ledger,
           wrong_tax_split)
        values (%s, %s, %s, %s, %s, %s, %s)
        returning *
        """,
        (
            record_id,
            voucher_id,
            J(human_payload),
            J(field_diffs),
            match_score,
            wrong_ledger,
            wrong_tax_split,
        ),
    )
    return await cur.fetchone()


async def find_compliance_record(
    cur: psycopg.Cursor, *, record_id: uuid.UUID
) -> dict[str, Any] | None:
    await cur.execute(
        "select * from compliance_records where record_id = %s limit 1", (record_id,)
    )
    return await cur.fetchone()
