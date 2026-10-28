"""Documents, extraction runs, records, field values and rule results.

Also the read side the router needs: duplicate detection, purchase-order and goods
receipt matching, GSTR-2B comparison, ledger mappings and open periods.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from decimal import Decimal
from typing import Any, Iterable, Sequence

import psycopg

from engine.db import J
from engine.models import ExtractedRecord, FieldValue, ValidationSummary
from engine.packs.loader import Pack
from engine.validation.lookups import (
    DuplicateFinding,
    EwayBillFinding,
    Gstr2bFinding,
    LedgerMapping,
    LookupBundle,
    ThreeWayFinding,
)
from engine.validation.gstn import GstinStatus, normalise_gstin


def normalise_name(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"[^a-z0-9 ]", " ", str(value).lower())
    text = re.sub(
        r"\b(pvt|private|ltd|limited|llp|inc|co|company|and|&)\b", " ", text
    )
    return re.sub(r"\s+", " ", text).strip()


def business_key(doc_type: str, payload: dict[str, Any]) -> str | None:
    """The key-field identity of a document, used for exact duplicate detection."""
    vendor = normalise_gstin(payload.get("vendor_gstin")) or normalise_name(
        payload.get("vendor_name")
    )
    if doc_type == "supplier_invoice":
        number = str(payload.get("invoice_number") or "").strip().upper()
        if not (vendor and number):
            return None
        return f"{doc_type}|{vendor}|{number}"
    if doc_type == "purchase_order":
        number = str(payload.get("po_number") or "").strip().upper()
        return f"{doc_type}|{vendor}|{number}" if number else None
    if doc_type == "goods_receipt":
        number = str(payload.get("challan_number") or "").strip().upper()
        return f"{doc_type}|{vendor}|{number}" if number else None
    if doc_type in {"e_way_bill", "e_invoice"}:
        number = payload.get("eway_bill_number") or payload.get("irn")
        return f"{doc_type}|{str(number).strip().upper()}" if number else None
    if doc_type == "bank_statement":
        reference = payload.get("reference")
        amount = payload.get("debit") or payload.get("credit")
        date = payload.get("date")
        account = payload.get("account_reference")
        if not (date and amount):
            return None
        return f"{doc_type}|{account}|{date}|{amount}|{reference or ''}"
    return None


# ── intake ──────────────────────────────────────────────────────────────────


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def find_document_by_hash(cur: psycopg.Cursor, file_hash: str) -> dict[str, Any] | None:
    await cur.execute(
        "select id, doc_type, received_at from documents where file_hash = %s "
        "order by received_at limit 1",
        (file_hash,),
    )
    return await cur.fetchone()


async def insert_document(
    cur: psycopg.Cursor,
    *,
    intake_channel: str,
    file_name: str | None,
    mime_type: str | None,
    file_size: int | None,
    file_hash: str,
    storage_path: str | None = None,
    source_ref: str | None = None,
    sender: str | None = None,
    page_count: int | None = None,
    duplicate_of: uuid.UUID | None = None,
    split_from: uuid.UUID | None = None,
    split_index: int | None = None,
    raw_meta: dict[str, Any] | None = None,
    pack_key: str | None = None,
) -> dict[str, Any]:
    status = "deduplicated" if duplicate_of else "received"
    await cur.execute(
        """
        insert into documents
          (intake_channel, source_ref, sender, file_name, mime_type, file_size, page_count,
           file_hash, storage_path, status, duplicate_of, split_from, split_index, pack_key,
           raw_meta)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning *
        """,
        (
            intake_channel, source_ref, sender, file_name, mime_type, file_size, page_count,
            file_hash, storage_path, status, duplicate_of, split_from, split_index, pack_key,
            J(raw_meta or {}),
        ),
    )
    return await cur.fetchone()


async def set_document_classification(
    cur: psycopg.Cursor,
    document_id: uuid.UUID,
    *,
    doc_type: str | None,
    confidence: float | None,
    pack_key: str,
    pack_version: str,
    page_count: int | None = None,
) -> None:
    await cur.execute(
        """
        update documents
           set doc_type = %s, doc_type_confidence = %s, pack_key = %s, pack_version = %s,
               page_count = coalesce(%s, page_count)
         where id = %s
        """,
        (doc_type, confidence, pack_key, pack_version, page_count, document_id),
    )


async def set_document_status(
    cur: psycopg.Cursor,
    document_id: uuid.UUID,
    status: str,
    *,
    exception_reason: str | None = None,
    processed: bool = False,
) -> None:
    await cur.execute(
        """
        update documents
           set status = %s,
               exception_reason = coalesce(%s, exception_reason),
               processed_at = case when %s then now() else processed_at end
         where id = %s
        """,
        (status, exception_reason, processed, document_id),
    )


async def insert_pages(
    cur: psycopg.Cursor,
    document_id: uuid.UUID,
    pages: Sequence[dict[str, Any]],
) -> None:
    for page in pages:
        await cur.execute(
            """
            insert into document_pages
              (document_id, page_no, width, height, has_text_layer, image_hash, text_digest, meta)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (document_id, page_no) do nothing
            """,
            (
                document_id,
                page.get("page_no", 1),
                page.get("width"),
                page.get("height"),
                page.get("has_text_layer", True),
                page.get("image_hash"),
                page.get("text_digest"),
                J(page.get("meta", {})),
            ),
        )


# ── extraction ──────────────────────────────────────────────────────────────


async def start_extraction_run(
    cur: psycopg.Cursor,
    *,
    document_id: uuid.UUID,
    kind: str = "extract",
    pack_key: str,
    pack_version: str,
    model: str | None,
    prompt_version: str | None,
    attempt: int = 1,
    trace_id: str | None = None,
) -> uuid.UUID:
    await cur.execute(
        """
        insert into extraction_runs
          (document_id, kind, pack_key, pack_version, model, prompt_version, attempt, trace_id)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (document_id, kind, pack_key, pack_version, model, prompt_version, attempt, trace_id),
    )
    row = await cur.fetchone()
    return row["id"]


async def finish_extraction_run(
    cur: psycopg.Cursor,
    run_id: uuid.UUID,
    *,
    status: str,
    schema_ok: bool,
    record_count: int = 0,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
    latency_ms: int | None = None,
    error_text: str | None = None,
) -> None:
    await cur.execute(
        """
        update extraction_runs
           set status = %s, schema_ok = %s, tokens_in = %s, tokens_out = %s, cost_usd = %s,
               latency_ms = %s, error_text = %s, finished_at = now()
         where id = %s
        """,
        (status, schema_ok, tokens_in, tokens_out, cost_usd, latency_ms, error_text, run_id),
    )


def _record_columns(record: ExtractedRecord) -> dict[str, Any]:
    payload = record.payload
    low = record.low_confidence(0.0, mandatory_only=False)
    return {
        "doc_type": record.doc_type,
        "pack_key": record.pack_key,
        "pack_version": record.pack_version,
        "order_index": record.order_index,
        "page_from": record.page_from,
        "page_to": record.page_to,
        "payload": payload,
        "overall_confidence": record.overall_confidence,
        "min_field_confidence": record.min_field_confidence,
        "field_count": len(record.fields),
        "low_confidence_fields": [
            f.path for f in record.fields if f.confidence < 0.93 and f.mandatory
        ],
        "amount_total": payload.get("total") or payload.get("value") or payload.get("agreed_rate_total"),
        "vendor_gstin": normalise_gstin(payload.get("vendor_gstin") or payload.get("supplier_gstin")),
        "vendor_name": payload.get("vendor_name"),
        "document_number": (
            payload.get("invoice_number")
            or payload.get("po_number")
            or payload.get("challan_number")
            or payload.get("eway_bill_number")
            or payload.get("irn")
            or payload.get("reference")
        ),
        "document_date": (
            payload.get("invoice_date")
            or payload.get("po_date")
            or payload.get("challan_date")
            or payload.get("generation_date")
            or payload.get("date")
        ),
        "business_key": business_key(record.doc_type, payload),
    }


async def insert_record(
    cur: psycopg.Cursor,
    *,
    document_id: uuid.UUID,
    run_id: uuid.UUID | None,
    record: ExtractedRecord,
) -> dict[str, Any]:
    columns = _record_columns(record)
    await cur.execute(
        """
        insert into extracted_records
          (document_id, run_id, doc_type, pack_key, pack_version, order_index, page_from, page_to,
           payload, overall_confidence, min_field_confidence, field_count, low_confidence_fields,
           amount_total, vendor_gstin, vendor_name, document_number, document_date, business_key)
        values (%(document_id)s, %(run_id)s, %(doc_type)s, %(pack_key)s, %(pack_version)s,
                %(order_index)s, %(page_from)s, %(page_to)s, %(payload)s, %(overall_confidence)s,
                %(min_field_confidence)s, %(field_count)s, %(low_confidence_fields)s,
                %(amount_total)s, %(vendor_gstin)s, %(vendor_name)s, %(document_number)s,
                %(document_date)s, %(business_key)s)
        returning *
        """,
        {
            "document_id": document_id,
            "run_id": run_id,
            "payload": J(columns.pop("payload")),
            **columns,
        },
    )
    return await cur.fetchone()


async def insert_field_values(
    cur: psycopg.Cursor, record_id: uuid.UUID, fields: Iterable[FieldValue]
) -> None:
    for field in fields:
        await cur.execute(
            """
            insert into field_values
              (record_id, path, label, value_text, value_num, value_date, value_json, confidence,
               page_no, bbox, source_span, mandatory, corrected, corrected_by)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (record_id, path) do update
              set value_text = excluded.value_text,
                  confidence = excluded.confidence,
                  corrected = excluded.corrected
            """,
            (
                record_id,
                field.path,
                field.label,
                field.value_text,
                field.value_num,
                field.value_date,
                J(field.value_json) if field.value_json is not None else None,
                field.confidence,
                field.page_no,
                J(field.bbox) if field.bbox is not None else None,
                field.source_span,
                field.mandatory,
                field.corrected,
                field.corrected_by,
            ),
        )


async def insert_rule_results(
    cur: psycopg.Cursor, record_id: uuid.UUID, summary: ValidationSummary, pack: Pack
) -> None:
    for result in summary.results:
        await cur.execute(
            """
            insert into rule_results
              (record_id, rule_key, rule_version, pack_key, pack_version, severity, outcome,
               title, message, details, fields, degraded, recheck_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (record_id, rule_key) do update
              set outcome = excluded.outcome, message = excluded.message,
                  details = excluded.details, degraded = excluded.degraded,
                  recheck_at = excluded.recheck_at, checked_at = now()
            """,
            (
                record_id,
                result.rule_key,
                result.rule_version,
                pack.key,
                pack.version,
                result.severity,
                result.outcome,
                result.title,
                result.message,
                J(result.details),
                result.fields,
                result.degraded,
                result.recheck_at,
            ),
        )


async def set_record_status(
    cur: psycopg.Cursor, record_id: uuid.UUID, status: str
) -> None:
    await cur.execute("update extracted_records set status = %s where id = %s", (status, record_id))


# ── side tables per document type ───────────────────────────────────────────


async def link_side_tables(
    cur: psycopg.Cursor, record_row: dict[str, Any], record: ExtractedRecord
) -> None:
    """Mirror the record into the table that matching and compliance read."""
    payload = record.payload
    record_id = record_row["id"]
    document_id = record_row["document_id"]

    if record.doc_type == "purchase_order":
        await cur.execute(
            """
            insert into purchase_orders
              (record_id, po_number, po_date, vendor_gstin, vendor_name, lines,
               agreed_rate_total, delivery_date, payment_terms)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record_id, payload.get("po_number"), payload.get("po_date"),
                normalise_gstin(payload.get("vendor_gstin")), payload.get("vendor_name"),
                J(payload.get("line_items") or []), payload.get("agreed_rate_total"),
                payload.get("delivery_date"), payload.get("payment_terms"),
            ),
        )
    elif record.doc_type == "goods_receipt":
        await cur.execute(
            """
            insert into goods_receipts
              (record_id, challan_number, challan_date, vendor_gstin, vendor_name, po_number,
               lines, quantity_received, vehicle_number)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record_id, payload.get("challan_number"), payload.get("challan_date"),
                normalise_gstin(payload.get("vendor_gstin")), payload.get("vendor_name"),
                payload.get("po_reference"), J(payload.get("line_items") or []),
                payload.get("quantity_received"), payload.get("vehicle_number"),
            ),
        )
    elif record.doc_type == "e_way_bill":
        await cur.execute(
            """
            insert into compliance_records
              (record_id, kind, number, document_number, generated_on, supplier_gstin,
               recipient_gstin, hsn, total_value, vehicle_number, valid_upto)
            values (%s, 'e_way_bill', %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record_id, payload.get("eway_bill_number"), payload.get("document_number"),
                payload.get("generation_date"), normalise_gstin(payload.get("supplier_gstin")),
                normalise_gstin(payload.get("recipient_gstin")), payload.get("hsn"),
                payload.get("value"), payload.get("vehicle_number"), payload.get("valid_upto"),
            ),
        )
    elif record.doc_type == "e_invoice":
        await cur.execute(
            """
            insert into compliance_records
              (record_id, kind, number, acknowledgement_no, acknowledgement_date, qr_data,
               supplier_gstin, recipient_gstin, document_number, total_value)
            values (%s, 'e_invoice', %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record_id, payload.get("irn"), payload.get("acknowledgement_number"),
                payload.get("acknowledgement_date"), payload.get("qr_code_data"),
                normalise_gstin(payload.get("supplier_gstin")),
                normalise_gstin(payload.get("buyer_gstin")), payload.get("document_number"),
                payload.get("value"),
            ),
        )
    elif record.doc_type == "bank_statement":
        debit = payload.get("debit")
        credit = payload.get("credit")
        direction = "debit" if debit is not None else ("credit" if credit is not None else None)
        await cur.execute(
            """
            insert into bank_statement_lines
              (document_id, record_id, account_ref, txn_date, narration, debit, credit,
               direction, balance, reference)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                document_id, record_id, payload.get("account_reference"), payload.get("date"),
                payload.get("narration"), debit, credit, direction, payload.get("balance"),
                payload.get("reference"),
            ),
        )


# ── lookups for the rules engine ────────────────────────────────────────────


async def find_duplicate(
    cur: psycopg.Cursor,
    record: ExtractedRecord,
    *,
    similarity_threshold: float = 0.93,
    lookback_days: int = 400,
    exclude_record_id: uuid.UUID | None = None,
) -> DuplicateFinding:
    """Exact key-field match first, then a near match."""
    key = business_key(record.doc_type, record.payload)
    if key:
        await cur.execute(
            """
            select id, document_number, document_date
              from extracted_records
             where business_key = %s
               and (%s::uuid is null or id <> %s::uuid)
             limit 1
            """,
            (key, exclude_record_id, exclude_record_id),
        )
        row = await cur.fetchone()
        if row:
            return DuplicateFinding(
                exact=True,
                similarity=1.0,
                matched_record_id=row["id"],
                matched_document_number=row["document_number"],
                matched_date=row["document_date"],
                method="key_fields",
            )

    if record.doc_type in {"supplier_invoice", "supplier_credit_note"}:
        vendor = normalise_gstin(record.payload.get("vendor_gstin"))
        total = record.payload.get("total")
        number = str(record.payload.get("invoice_number") or "").strip().upper()
        if vendor and total is not None:
            await cur.execute(
                """
                select id, document_number, document_date, amount_total,
                       case when embedding is not null then 1 else 0 end as had_embedding
                  from extracted_records
                 where doc_type = %s
                   and vendor_gstin = %s
                   and amount_total = %s
                   and upper(coalesce(document_number, '')) <> %s
                   and created_at > now() - make_interval(days => %s)
                 order by created_at desc
                 limit 1
                """,
                (record.doc_type, vendor, total, number, lookback_days),
            )
            row = await cur.fetchone()
            if row:
                # Same vendor, same value, different number: the usual shape of a
                # re-keyed invoice, so flag it for a look rather than blocking.
                return DuplicateFinding(
                    exact=False,
                    similarity=0.9,
                    matched_record_id=row["id"],
                    matched_document_number=row["document_number"],
                    matched_date=row["document_date"],
                    method="vendor_value_date",
                )

    embedding = _embedding_of(record)
    if embedding is not None:
        await cur.execute(
            """
            select id, document_number, document_date,
                   1 - (embedding <=> %s::vector) as similarity
              from extracted_records
             where embedding is not null
               and created_at > now() - make_interval(days => %s)
               and (%s::uuid is null or id <> %s::uuid)
             order by embedding <=> %s::vector
             limit 1
            """,
            (
                _vector_literal(embedding), lookback_days, exclude_record_id,
                exclude_record_id, _vector_literal(embedding),
            ),
        )
        row = await cur.fetchone()
        if row and float(row["similarity"] or 0) >= similarity_threshold:
            return DuplicateFinding(
                exact=False,
                similarity=round(float(row["similarity"]), 4),
                matched_record_id=row["id"],
                matched_document_number=row["document_number"],
                matched_date=row["document_date"],
                method="pgvector",
            )

    return DuplicateFinding(exact=False, similarity=0.0, method="none")


def _embedding_of(record: ExtractedRecord) -> list[float] | None:
    value = record.payload.get("_embedding") if isinstance(record.payload, dict) else None
    return value if isinstance(value, list) and value else None


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


async def load_ledger_mapping(
    cur: psycopg.Cursor, *, gstin: str | None, name: str | None
) -> LedgerMapping | None:
    """Match the supplier by GSTIN first, then by normalised name (Appendix C)."""
    gstin = normalise_gstin(gstin)
    if gstin:
        await cur.execute("select * from ledger_mappings where party_gstin = %s limit 1", (gstin,))
        row = await cur.fetchone()
        if row:
            return _mapping_from_row(row)
    if name:
        await cur.execute(
            "select * from ledger_mappings where party_name_norm = %s order by use_count desc limit 1",
            (normalise_name(name),),
        )
        row = await cur.fetchone()
        if row:
            return _mapping_from_row(row)
    return None


def _mapping_from_row(row: dict[str, Any]) -> LedgerMapping:
    return LedgerMapping(
        party_gstin=row["party_gstin"],
        party_name=row["party_name"],
        target_party_ledger=row["target_party_ledger"],
        purchase_ledger=row["purchase_ledger"],
        expense_ledger=row["expense_ledger"],
        cgst_ledger=row["cgst_ledger"],
        sgst_ledger=row["sgst_ledger"],
        igst_ledger=row["igst_ledger"],
        roundoff_ledger=row["roundoff_ledger"],
        tds_ledger=row["tds_ledger"],
        default_stock_item=row["default_stock_item"],
        confidence=float(row["confidence"] or 0),
        source=row["source"],
    )


async def upsert_ledger_mapping(
    cur: psycopg.Cursor,
    *,
    gstin: str | None,
    name: str | None,
    target_party_ledger: str | None = None,
    purchase_ledger: str | None = None,
    expense_ledger: str | None = None,
    cgst_ledger: str | None = None,
    sgst_ledger: str | None = None,
    igst_ledger: str | None = None,
    roundoff_ledger: str | None = None,
    tds_ledger: str | None = None,
    default_stock_item: str | None = None,
    confidence: float = 1.0,
    source: str = "manual",
    approved_by: str | None = None,
) -> dict[str, Any]:
    await cur.execute(
        """
        insert into ledger_mappings
          (party_gstin, party_name, party_name_norm, target_party_ledger, purchase_ledger,
           expense_ledger, cgst_ledger, sgst_ledger, igst_ledger, roundoff_ledger, tds_ledger,
           default_stock_item, confidence, source, approved_by, approved_at)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        on conflict (party_gstin, party_name_norm) do update
          set target_party_ledger = coalesce(excluded.target_party_ledger, ledger_mappings.target_party_ledger),
              purchase_ledger = coalesce(excluded.purchase_ledger, ledger_mappings.purchase_ledger),
              expense_ledger = coalesce(excluded.expense_ledger, ledger_mappings.expense_ledger),
              cgst_ledger = coalesce(excluded.cgst_ledger, ledger_mappings.cgst_ledger),
              sgst_ledger = coalesce(excluded.sgst_ledger, ledger_mappings.sgst_ledger),
              igst_ledger = coalesce(excluded.igst_ledger, ledger_mappings.igst_ledger),
              roundoff_ledger = coalesce(excluded.roundoff_ledger, ledger_mappings.roundoff_ledger),
              tds_ledger = coalesce(excluded.tds_ledger, ledger_mappings.tds_ledger),
              default_stock_item = coalesce(excluded.default_stock_item, ledger_mappings.default_stock_item),
              confidence = greatest(excluded.confidence, ledger_mappings.confidence),
              source = excluded.source,
              approved_by = coalesce(excluded.approved_by, ledger_mappings.approved_by)
        returning *
        """,
        (
            normalise_gstin(gstin), name, normalise_name(name), target_party_ledger, purchase_ledger,
            expense_ledger, cgst_ledger, sgst_ledger, igst_ledger, roundoff_ledger, tds_ledger,
            default_stock_item, confidence, source, approved_by,
        ),
    )
    return await cur.fetchone()


async def load_open_periods(cur: psycopg.Cursor) -> list[tuple[dt.date, dt.date]]:
    """Open financial periods reported by the bridge agent's master sync."""
    await cur.execute(
        """
        select payload from master_snapshots
         where kind = 'periods'
         order by captured_at desc
         limit 1
        """
    )
    row = await cur.fetchone()
    if not row:
        return []
    periods: list[tuple[dt.date, dt.date]] = []
    for item in row["payload"] or []:
        try:
            periods.append(
                (dt.date.fromisoformat(item["from"]), dt.date.fromisoformat(item["to"]))
            )
        except (KeyError, TypeError, ValueError):
            continue
    return periods


async def three_way_match(cur: psycopg.Cursor, record: ExtractedRecord) -> ThreeWayFinding:
    """Invoice against purchase order against goods receipt (6.3)."""
    payload = record.payload
    po_number = payload.get("po_reference")
    finding = ThreeWayFinding(po_number=po_number)
    if not po_number:
        return finding

    await cur.execute(
        "select * from purchase_orders where po_number = %s order by created_at desc limit 1",
        (po_number,),
    )
    po_row = await cur.fetchone()
    if not po_row:
        return finding

    finding.po_found = True
    await cur.execute(
        "select * from goods_receipts where po_number = %s or challan_number = %s "
        "order by created_at desc limit 1",
        (po_number, payload.get("challan_reference")),
    )
    grn_row = await cur.fetchone()
    finding.goods_receipt_found = bool(grn_row)

    invoice_qty, invoice_rate = _totals(payload.get("line_items") or [])
    po_qty, po_rate = _totals(po_row["lines"] or [])
    finding.quantity_variance_pct = _variance(invoice_qty, po_qty)
    finding.rate_variance_pct = _variance(invoice_rate, po_rate)

    if grn_row:
        grn_qty, _ = _totals(grn_row["lines"] or [], quantity_key="quantity_received")
        received = grn_row.get("quantity_received") or grn_qty
        if received and invoice_qty:
            grn_variance = _variance(invoice_qty, Decimal(str(received)))
            if grn_variance is not None:
                existing = finding.quantity_variance_pct
                if existing is None or abs(grn_variance) > abs(existing):
                    finding.quantity_variance_pct = grn_variance
    return finding


def _totals(lines: Sequence[dict[str, Any]], quantity_key: str = "quantity") -> tuple[Decimal, Decimal]:
    quantity = Decimal("0")
    amount = Decimal("0")
    for line in lines:
        try:
            quantity += Decimal(str(line.get(quantity_key) or 0))
        except Exception:
            pass
        try:
            amount += Decimal(str(line.get("amount") or 0))
        except Exception:
            pass
    return quantity, amount


def _variance(actual: Decimal, expected: Decimal) -> Decimal | None:
    if not expected:
        return None
    return ((actual - expected) / expected * Decimal("100")).quantize(Decimal("0.01"))


async def gstr2b_lookup(cur: psycopg.Cursor, record: ExtractedRecord) -> Gstr2bFinding | None:
    """Whether the invoice shows up in the vendor's filed return."""
    number = record.payload.get("invoice_number")
    vendor = normalise_gstin(record.payload.get("vendor_gstin"))
    if not number or not vendor:
        return None

    await cur.execute(
        """
        select * from compliance_records
         where kind = 'gstr2b' and document_number = %s and supplier_gstin = %s
         order by created_at desc limit 1
        """,
        (number, vendor),
    )
    row = await cur.fetchone()
    if not row:
        return Gstr2bFinding(found=False, detail=(
            "This invoice is not present in the GSTR-2B data loaded for the period, so "
            "input credit is at risk."
        ))

    total = record.payload.get("total")
    value_diff = None
    if total is not None and row["total_value"] is not None:
        value_diff = Decimal(str(total)) - Decimal(str(row["total_value"]))
    return Gstr2bFinding(
        found=True,
        period=row["period"],
        value_difference=value_diff,
        tax_difference=(
            Decimal(str(row["match_detail"].get("tax_difference", 0)))
            if row["match_detail"]
            else None
        ),
    )


async def eway_bill_lookup(cur: psycopg.Cursor, record: ExtractedRecord) -> EwayBillFinding | None:
    number = record.payload.get("eway_bill_number")
    if not number:
        return None
    await cur.execute(
        """
        select * from compliance_records
         where kind = 'e_way_bill' and number = %s
         order by created_at desc limit 1
        """,
        (number,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    expired = bool(row.get("valid_upto") and row["valid_upto"] < dt.date.today())
    return EwayBillFinding(
        number=row["number"],
        value=row["total_value"],
        supplier_gstin=row["supplier_gstin"],
        recipient_gstin=row["recipient_gstin"],
        document_number=row["document_number"],
        valid_upto=row["valid_upto"],
        expired=expired,
    )


async def previous_bank_balance(cur: psycopg.Cursor, record: ExtractedRecord) -> Decimal | None:
    explicit = record.payload.get("_previous_balance")
    if explicit is not None:
        try:
            return Decimal(str(explicit))
        except Exception:
            pass
    await cur.execute(
        """
        select balance from bank_statement_lines
         where account_ref = %s and txn_date < %s and balance is not null
         order by txn_date desc, created_at desc
         limit 1
        """,
        (record.payload.get("account_reference"), record.payload.get("date")),
    )
    row = await cur.fetchone()
    return row["balance"] if row else None


async def build_lookups(
    cur: psycopg.Cursor,
    record: ExtractedRecord,
    *,
    gstn_status: dict[str, GstinStatus] | None = None,
    e_invoicing_applies: bool = False,
    exclude_record_id: uuid.UUID | None = None,
) -> LookupBundle:
    """Everything the checks may consult, fetched in one place."""
    bundle = LookupBundle(
        gstin_status=gstn_status or {},
        e_invoicing_applies=e_invoicing_applies,
        open_periods=await load_open_periods(cur),
    )
    bundle.duplicate = await find_duplicate(cur, record, exclude_record_id=exclude_record_id)
    bundle.ledger_mapping = await load_ledger_mapping(
        cur,
        gstin=record.payload.get("vendor_gstin"),
        name=record.payload.get("vendor_name"),
    )
    if record.doc_type == "supplier_invoice":
        bundle.three_way = await three_way_match(cur, record)
        bundle.gstr2b = await gstr2b_lookup(cur, record)
        bundle.eway_bill = await eway_bill_lookup(cur, record)
    if record.doc_type == "bank_statement":
        bundle.previous_balance = await previous_bank_balance(cur, record)
    return bundle
