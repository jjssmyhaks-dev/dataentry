"""The review queue (Appendix D): items, actions, metrics and the learning loop."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Sequence

import psycopg

from engine.db import J
from engine.models import ExtractedRecord, RoutingDecision
from engine.repo.tenant import normalise_name
from engine.routing.autonomy import AutonomyMetrics


async def insert_review_item(
    cur: psycopg.Cursor,
    *,
    record_row: dict[str, Any],
    record: ExtractedRecord,
    decision: RoutingDecision,
    status: str = "pending",
) -> dict[str, Any]:
    await cur.execute(
        """
        insert into review_items
          (record_id, document_id, status, priority, priority_band, reasons, flagged_fields,
           failed_rules, warned_rules, amount_total, doc_type, vendor_name,
           requires_supervisor)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning *
        """,
        (
            record_row["id"],
            record_row["document_id"],
            status,
            decision.priority,
            decision.priority_band,
            J([reason.model_dump() for reason in decision.reasons]),
            sorted({field for reason in decision.reasons for field in reason.fields}),
            [r.rule_key for r in decision.reasons if r.rule_key and r.code.startswith("rule_failed")],
            [r.rule_key for r in decision.reasons if r.rule_key and r.code.startswith("rule_warned")],
            record_row.get("amount_total"),
            record.doc_type,
            record_row.get("vendor_name"),
            decision.requires_supervisor,
        ),
    )
    return await cur.fetchone()


async def record_auto_post_decision(
    cur: psycopg.Cursor,
    *,
    record_row: dict[str, Any],
    record: ExtractedRecord,
    decision: RoutingDecision,
) -> dict[str, Any]:
    """Auto-posted records still get a queue row, resolved, so the activity list
    and the accuracy dashboards have one place to read from."""
    item = await insert_review_item(
        cur,
        record_row=record_row,
        record=record,
        decision=decision,
        status="auto_posted",
    )
    await cur.execute(
        """
        update review_items
           set resolution = %s, resolution_note = %s, resolved_at = now()
         where id = %s
        """,
        (
            "auto_posted",
            decision.reasons[0].message if decision.reasons else None,
            item["id"],
        ),
    )
    return item


async def queue_list(
    cur: psycopg.Cursor,
    *,
    statuses: Sequence[str] = ("pending", "held", "clarification_requested"),
    doc_type: str | None = None,
    vendor: str | None = None,
    reason: str | None = None,
    assignee: str | None = None,
    band: str | None = None,
    min_amount: Decimal | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses = ["ri.status = any(%s)"]
    params: list[Any] = [list(statuses)]
    if doc_type:
        clauses.append("ri.doc_type = %s")
        params.append(doc_type)
    if vendor:
        clauses.append("ri.vendor_name ilike %s")
        params.append(f"%{vendor}%")
    if reason:
        clauses.append("exists (select 1 from jsonb_array_elements(ri.reasons) r where r->>'code' like %s)")
        params.append(f"%{reason}%")
    if assignee:
        clauses.append("ri.assignee = %s")
        params.append(assignee)
    if band:
        clauses.append("ri.priority_band = %s")
        params.append(band)
    if min_amount is not None:
        clauses.append("ri.amount_total >= %s")
        params.append(min_amount)

    params.extend([limit, offset])
    await cur.execute(
        f"""
        select ri.*, er.overall_confidence, er.status as record_status,
               d.file_name, d.doc_type as document_doc_type, d.received_at,
               (select count(*) from vouchers v where v.record_id = ri.record_id) as voucher_count
          from review_items ri
          join extracted_records er on er.id = ri.record_id
          join documents d on d.id = ri.document_id
         where {' and '.join(clauses)}
         order by ri.priority asc, ri.created_at asc
         limit %s offset %s
        """,
        tuple(params),
    )
    return list(await cur.fetchall())


async def queue_counts(cur: psycopg.Cursor) -> dict[str, int]:
    await cur.execute(
        """
        select status, count(*) as n,
               count(*) filter (where priority_band = 'rule_failure') as rule_failures,
               coalesce(sum(amount_total), 0) as value
          from review_items
         group by status
        """
    )
    rows = await cur.fetchall()
    return {
        row["status"]: {
            "count": int(row["n"]),
            "rule_failures": int(row["rule_failures"]),
            "value": float(row["value"]),
        }
        for row in rows
    }


async def review_item_detail(cur: psycopg.Cursor, item_id: uuid.UUID) -> dict[str, Any] | None:
    await cur.execute("select * from review_items where id = %s", (item_id,))
    item = await cur.fetchone()
    if not item:
        return None

    await cur.execute("select * from extracted_records where id = %s", (item["record_id"],))
    record = await cur.fetchone()
    await cur.execute(
        "select * from field_values where record_id = %s order by mandatory desc, confidence asc",
        (item["record_id"],),
    )
    fields = list(await cur.fetchall())
    await cur.execute(
        "select * from rule_results where record_id = %s "
        "order by case outcome when 'fail' then 0 when 'warn' then 1 when 'skipped' then 3 else 2 end",
        (item["record_id"],),
    )
    rules = list(await cur.fetchall())
    await cur.execute("select * from documents where id = %s", (item["document_id"],))
    document = await cur.fetchone()
    # The review pane shows the page text so a reviewer never has to open the
    # original in another window (Appendix D).
    await cur.execute(
        "select page_no, has_text_layer, "
        "coalesce(nullif(text_digest, ''), meta->>'text') as text_digest, width, height "
        "from document_pages where document_id = %s order by page_no",
        (item["document_id"],),
    )
    pages = list(await cur.fetchall())
    await cur.execute(
        "select * from vouchers where record_id = %s order by created_at desc limit 1",
        (item["record_id"],),
    )
    voucher = await cur.fetchone()
    await cur.execute(
        "select * from review_actions where review_item_id = %s order by created_at",
        (item_id,),
    )
    actions = list(await cur.fetchall())

    hidden = {k for k in (record or {}) if k == "embedding"}
    return {
        "item": item,
        "record": {k: v for k, v in (record or {}).items() if k not in hidden},
        "fields": fields,
        "rules": rules,
        "document": document,
        "pages": pages,
        "voucher": voucher,
        "actions": actions,
    }


async def insert_review_action(
    cur: psycopg.Cursor,
    *,
    item_id: uuid.UUID,
    record_id: uuid.UUID,
    actor: str,
    actor_role: str | None,
    action: str,
    field_path: str | None = None,
    before_value: str | None = None,
    after_value: str | None = None,
    note: str | None = None,
    is_one_off: bool = False,
) -> dict[str, Any]:
    await cur.execute(
        """
        insert into review_actions
          (review_item_id, record_id, actor, actor_role, action, field_path, before_value,
           after_value, note, is_one_off)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning *
        """,
        (
            item_id, record_id, actor, actor_role, action, field_path, before_value,
            after_value, note, is_one_off,
        ),
    )
    return await cur.fetchone()


async def resolve_review_item(
    cur: psycopg.Cursor,
    item_id: uuid.UUID,
    *,
    status: str,
    resolution: str | None,
    reason: str | None,
    note: str | None,
    actor: str,
) -> dict[str, Any] | None:
    await cur.execute(
        """
        update review_items
           set status = %s, resolution = %s, resolution_reason = %s, resolution_note = %s,
               resolved_by = %s, resolved_at = now()
         where id = %s
        returning *
        """,
        (status, resolution, reason, note, actor, item_id),
    )
    return await cur.fetchone()


async def hold_review_item(
    cur: psycopg.Cursor, item_id: uuid.UUID, *, note: str, actor: str
) -> dict[str, Any] | None:
    await cur.execute(
        """
        update review_items
           set status = 'held', resolution = 'held', resolution_note = %s, assignee = coalesce(assignee, %s)
         where id = %s
        returning *
        """,
        (note, actor, item_id),
    )
    return await cur.fetchone()


async def assign_review_item(
    cur: psycopg.Cursor, item_id: uuid.UUID, *, assignee: str, actor: str
) -> dict[str, Any] | None:
    await cur.execute(
        "update review_items set assignee = %s where id = %s returning *",
        (assignee, item_id),
    )
    return await cur.fetchone()


async def insert_clarification_request(
    cur: psycopg.Cursor,
    *,
    item_id: uuid.UUID | None,
    document_id: uuid.UUID,
    channel: str,
    to_address: str | None,
    template_key: str,
    body: str,
) -> dict[str, Any]:
    await cur.execute(
        """
        insert into clarification_requests
          (review_item_id, document_id, channel, to_address, template_key, body)
        values (%s, %s, %s, %s, %s, %s)
        returning *
        """,
        (item_id, document_id, channel, to_address, template_key, body),
    )
    return await cur.fetchone()


# ── learning loop (6.6) ─────────────────────────────────────────────────────


async def insert_correction(
    cur: psycopg.Cursor,
    *,
    record_id: uuid.UUID,
    document_id: uuid.UUID | None,
    field_path: str,
    before_value: str | None,
    after_value: str | None,
    vendor_gstin: str | None,
    vendor_name: str | None,
    doc_type: str | None,
    actor: str,
    is_one_off: bool = False,
) -> dict[str, Any]:
    await cur.execute(
        """
        insert into corrections
          (record_id, document_id, field_path, before_value, after_value, vendor_gstin,
           vendor_name, doc_type, actor, is_one_off)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning *
        """,
        (
            record_id, document_id, field_path, before_value, after_value, vendor_gstin,
            vendor_name, doc_type, actor, is_one_off,
        ),
    )
    return await cur.fetchone()


async def count_repeated_corrections(
    cur: psycopg.Cursor, *, vendor_gstin: str | None, field_path: str, min_count: int = 3
) -> int:
    await cur.execute(
        """
        select count(*) as n from corrections
         where field_path = %s and is_one_off = false
           and (%s::text is null or vendor_gstin = %s)
        """,
        (field_path, vendor_gstin, vendor_gstin),
    )
    row = await cur.fetchone()
    return int(row["n"] if row else 0)


async def upsert_rule_suggestion(
    cur: psycopg.Cursor,
    *,
    kind: str,
    vendor_gstin: str | None,
    doc_type: str | None,
    field_path: str | None,
    proposal: dict[str, Any],
) -> dict[str, Any]:
    await cur.execute(
        """
        insert into rule_suggestions (kind, vendor_gstin, doc_type, field_path, proposal)
        values (%s, %s, %s, %s, %s)
        returning *
        """,
        (kind, vendor_gstin, doc_type, field_path, J(proposal)),
    )
    return await cur.fetchone()


async def list_rule_suggestions(cur: psycopg.Cursor, *, status: str = "suggested") -> list[dict[str, Any]]:
    await cur.execute(
        "select * from rule_suggestions where status = %s order by occurrence_count desc, created_at desc",
        (status,),
    )
    return list(await cur.fetchall())


async def decide_rule_suggestion(
    cur: psycopg.Cursor, suggestion_id: uuid.UUID, *, accept: bool, actor: str
) -> dict[str, Any] | None:
    await cur.execute(
        """
        update rule_suggestions
           set status = %s, decided_by = %s, decided_at = now(), updated_at = now()
         where id = %s
        returning *
        """,
        ("accepted" if accept else "dismissed", actor, suggestion_id),
    )
    return await cur.fetchone()


async def add_few_shot_example(
    cur: psycopg.Cursor,
    *,
    doc_type: str,
    vendor_gstin: str | None,
    pack_key: str,
    input_excerpt: str,
    output_payload: dict[str, Any],
    source_record_id: uuid.UUID | None = None,
) -> None:
    await cur.execute(
        """
        insert into few_shot_examples
          (doc_type, vendor_gstin, pack_key, input_excerpt, output_payload, source_record_id)
        values (%s, %s, %s, %s, %s, %s)
        """,
        (doc_type, vendor_gstin, pack_key, input_excerpt, J(output_payload), source_record_id),
    )


# ── metrics (section 11 and the section 12a gates) ──────────────────────────


async def metrics(
    cur: psycopg.Cursor,
    *,
    days: int = 30,
    observation_days: int = 0,
) -> AutonomyMetrics:
    window = "now() - make_interval(days => %s)"

    await cur.execute(f"select count(*) as n from documents where received_at > {window}", (days,))
    documents = int((await cur.fetchone())["n"])

    await cur.execute(
        f"""
        select
          count(*) filter (where status in ('posted', 'reversed')) as posted,
          count(*) filter (where status = 'reversed') as reversed,
          count(*) filter (where amount_total > 100000) as high_value
        from vouchers
        where posting_mode = 'autonomous' and created_at > {window}
        """,
        (days,),
    )
    voucher_row = await cur.fetchone()

    await cur.execute(
        f"""
        select count(*) as n from vouchers
         where posting_mode = 'autonomous' and amount_total > 100000 and status = 'reversed'
           and created_at > {window}
        """,
        (days,),
    )
    high_value_errors = int((await cur.fetchone())["n"])

    await cur.execute(
        f"""
        select
          count(*) as corrected,
          count(*) filter (where reversed_at is null) as unreversed
        from vouchers v
        where v.posting_mode = 'autonomous'
          and v.created_at > {window}
          and exists (
            select 1 from corrections c
             where c.record_id = v.record_id and c.created_at > coalesce(v.posted_at, v.created_at)
          )
        """,
        (days,),
    )
    correction_row = await cur.fetchone()

    await cur.execute(
        f"""
        select
          count(*) filter (where field_path ilike '%%ledger%%') as wrong_ledger,
          count(*) filter (where field_path in ('cgst', 'sgst', 'igst', 'total')) as wrong_tax
        from corrections where created_at > {window}
        """,
        (days,),
    )
    wrong_row = await cur.fetchone()

    await cur.execute(
        f"""
        select
          count(*) as total,
          count(*) filter (where status in ('pending', 'held', 'clarification_requested')) as pending,
          count(*) filter (where status = 'approved') as approved_clean,
          count(*) filter (where resolved_at is not null) as resolved,
          percentile_cont(0.5) within group (
            order by extract(epoch from (resolved_at - created_at))
          ) filter (where resolved_at is not null) as median_seconds
        from review_items where created_at > {window}
        """,
        (days,),
    )
    review_row = await cur.fetchone()

    await cur.execute(
        f"""
        select
          count(*) as total,
          count(*) filter (where jsonb_array_length(field_diffs) = 0) as clean,
          coalesce(avg(match_score), null) as mean_score,
          count(*) filter (where reviewed and jsonb_array_length(field_diffs) > 0) as discrepancies
        from shadow_comparisons where created_at > {window}
        """,
        (days,),
    )
    shadow_row = await cur.fetchone()

    return AutonomyMetrics(
        documents_processed=documents,
        auto_posted=int(voucher_row["posted"] or 0),
        auto_posted_corrected=int(correction_row["corrected"] or 0),
        unreversed_errors=int(correction_row["unreversed"] or 0),
        high_value_auto_posted=int(voucher_row["high_value"] or 0),
        high_value_errors=high_value_errors,
        wrong_ledger_errors=int(wrong_row["wrong_ledger"] or 0),
        wrong_tax_split_errors=int(wrong_row["wrong_tax"] or 0),
        reversals=int(voucher_row["reversed"] or 0),
        review_items=int(review_row["total"] or 0),
        review_items_pending=int(review_row["pending"] or 0),
        median_review_seconds=(
            float(review_row["median_seconds"]) if review_row["median_seconds"] is not None else None
        ),
        reviewed_without_edits=int(review_row["approved_clean"] or 0),
        reviewed_total=int(review_row["resolved"] or 0),
        field_accuracy=(
            float(shadow_row["mean_score"]) if shadow_row["mean_score"] is not None else None
        ),
        shadow_documents=int(shadow_row["total"] or 0),
        shadow_discrepancies=int(shadow_row["discrepancies"] or 0),
        observation_days=observation_days,
    )


async def flag_reason_counts(cur: psycopg.Cursor, *, days: int = 30) -> list[dict[str, Any]]:
    """Most common flag reasons, so pack rules and extraction can be improved."""
    await cur.execute(
        """
        select r->>'code' as code, r->>'message' as message, count(*) as n
          from review_items ri, jsonb_array_elements(ri.reasons) r
         where ri.created_at > now() - make_interval(days => %s)
         group by 1, 2
         order by n desc
         limit 25
        """,
        (days,),
    )
    return list(await cur.fetchall())


async def accuracy_by_vendor(cur: psycopg.Cursor, *, days: int = 90) -> list[dict[str, Any]]:
    await cur.execute(
        """
        select
          coalesce(vendor_gstin, 'unknown') as vendor_gstin,
          max(vendor_name) as vendor_name,
          count(*) as documents,
          count(*) filter (where status in ('auto_posted', 'posted')) as auto_posted,
          count(*) filter (where status in ('rejected', 'reversed')) as problems,
          avg(overall_confidence) as mean_confidence
        from extracted_records
        where created_at > now() - make_interval(days => %s)
        group by 1
        having count(*) > 0
        order by problems desc, documents desc
        limit 50
        """,
        (days,),
    )
    return list(await cur.fetchall())


async def rule_settings(cur: psycopg.Cursor) -> dict[str, dict[str, Any]]:
    await cur.execute("select * from rule_settings")
    rows = await cur.fetchall()
    return {
        row["rule_key"]: {
            "enabled": row["enabled"],
            "severity_override": row["severity_override"],
            "settings": row["settings"] or {},
        }
        for row in rows
    }


async def upsert_rule_setting(
    cur: psycopg.Cursor,
    *,
    rule_key: str,
    doc_type: str | None,
    enabled: bool = True,
    severity_override: str | None = None,
    settings: dict[str, Any] | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    await cur.execute(
        """
        insert into rule_settings (rule_key, doc_type, enabled, severity_override, settings, updated_by)
        values (%s, %s, %s, %s, %s, %s)
        on conflict (rule_key, doc_type) do update
          set enabled = excluded.enabled,
              severity_override = excluded.severity_override,
              settings = excluded.settings,
              updated_by = excluded.updated_by,
              updated_at = now()
        returning *
        """,
        (rule_key, doc_type, enabled, severity_override, J(settings or {}), actor),
    )
    return await cur.fetchone()


async def fix_reason_for_vendor(cur: psycopg.Cursor, vendor_name: str | None) -> dict[str, Any] | None:
    """Per-tenant, per-vendor error rate, for the automatic demotion rule."""
    if not vendor_name:
        return None
    await cur.execute(
        """
        select
          count(*) as n,
          count(*) filter (where exists (
            select 1 from corrections c where c.record_id = er.id
          )) as corrected
        from extracted_records er
        where vendor_name = %s and doc_type = 'supplier_invoice'
          and created_at > now() - interval '90 days'
        """,
        (normalise_name(vendor_name) and vendor_name,),
    )
    return await cur.fetchone()
