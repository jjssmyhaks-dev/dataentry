"""Text-layer baseline extractor.

Two reasons this exists rather than being a stub:

1. Many real documents arrive as PDFs with a text layer, and a label/value or
   table layout can be read deterministically and cheaply, without a model call.
2. It gives the pipeline, the rules engine and the accuracy harness a floor to
   measure against: if a pack change makes things worse than the baseline, that
   is visible immediately.

It deliberately does not try to read handwriting, photographs or skewed scans:
those go to the vision model (``engine.extraction.agent``).
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from engine.extraction.classifier import DocumentSegment
from engine.models import ExtractedRecord, FieldValue
from engine.packs.loader import Pack
from engine.packs.models import DocumentTypeSpec, FieldSpec

UNREADABLE = {"", "-", "--", "?", "??", "n/a", "na", "nil", "illegible", "xxx", "xx"}

TABLE_HEADER_ALIASES: dict[str, str] = {
    "qty": "quantity",
    "quantity": "quantity",
    "nos": "quantity",
    "pcs": "quantity",
    "qty received": "quantity_received",
    "received qty": "quantity_received",
    "gst": "gst_rate",
    "gst%": "gst_rate",
    "gst rate": "gst_rate",
    "tax %": "gst_rate",
    "hsn": "hsn",
    "hsn/sac": "hsn",
    "sac": "hsn",
    "hsn code": "hsn",
    "rate": "rate",
    "unit price": "rate",
    "amount": "amount",
    "value": "amount",
    "description": "description",
    "description of goods": "description",
    "particulars": "description",
    "item": "description",
    "item description": "description",
    "unit": "unit",
    "uom": "unit",
    "discount": "discount",
    "withdrawal": "debit",
    "withdrawals": "debit",
    "debit": "debit",
    "dr": "debit",
    "deposit": "credit",
    "deposits": "credit",
    "credit": "credit",
    "cr": "credit",
    "balance": "balance",
    "narration": "narration",
    "remarks": "narration",
    "particulars/narration": "narration",
    "ref no": "reference",
    "reference": "reference",
    "cheque no": "reference",
    "utr": "reference",
    "date": "date",
    "txn date": "date",
    "value date": "date",
}

DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%d/%m/%y")


def normalise_label(text: str) -> str:
    text = text.split("@")[0]
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"[^0-9a-zA-Z]+", "", text)
    return text.lower()


def clean_number(text: Any) -> Decimal | None:
    if text is None:
        return None
    if isinstance(text, (int, float, Decimal)) and not isinstance(text, bool):
        return Decimal(str(text))
    raw = str(text).strip()
    if raw.lower() in UNREADABLE:
        return None
    raw = raw.replace("₹", "").replace("Rs.", "").replace("Rs", "").replace("INR", "")
    raw = re.sub(r"\s", "", raw)
    if raw in {"", "-", "--"}:
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()").replace(",", "")
    raw = re.sub(r"(?i)(kg|pcs|nos|mtr|mt|ltr|ltrs|set|box|job|hrs|hr)$", "", raw)
    if raw.endswith("%"):
        raw = raw[:-1]
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return -value if negative else value


def clean_date(text: Any) -> dt.date | None:
    raw = str(text or "").strip()
    if raw.lower() in UNREADABLE:
        return None
    raw = re.sub(r"\s+\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?$", "", raw, flags=re.I)
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def clean_gstin(text: Any) -> str | None:
    raw = re.sub(r"\s+", "", str(text or "")).upper()
    return raw or None


def clean_text(text: Any) -> str | None:
    raw = str(text or "").strip()
    if raw.lower() in UNREADABLE:
        return None
    return raw or None


# ── label/value parsing ─────────────────────────────────────────────────────


# Labels may carry a rate or qualifier: "CGST @ 9%", "IGST (18%)", "Taxable Value".
KV_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 ./&%()@*\-]{1,44}?)\s*[:#]\s*(.+?)\s*$")


def label_value_pairs(lines: list[str]) -> list[tuple[str, str, int]]:
    pairs: list[tuple[str, str, int]] = []
    for index, line in enumerate(lines):
        match = KV_RE.match(line)
        if not match:
            continue
        label, value = match.group(1), match.group(2)
        if len(value) > 200:
            continue
        pairs.append((label, value, index + 1))
    return pairs


def _label_keys(spec: FieldSpec) -> list[str]:
    keys = {normalise_label(spec.name)}
    if spec.label:
        keys.add(normalise_label(spec.label))
    for alias in spec.aliases:
        keys.add(normalise_label(alias))
    return sorted(k for k in keys if k)


def _match_field(label: str, specs: list[FieldSpec]) -> FieldSpec | None:
    key = normalise_label(label)
    if not key:
        return None
    for spec in specs:
        if key in _label_keys(spec):
            return spec
    # Prefix fallback for a label that carries extra words, so "HSN Code" finds
    # "hsn". Only this direction is allowed: matching a short label against a
    # longer key would make generic headings stick to whatever they prefix, which
    # is how a bare "State:" line ends up read as SGST via the "state tax" alias.
    best: tuple[int, FieldSpec] | None = None
    for spec in specs:
        for candidate in _label_keys(spec):
            if len(candidate) < 3 or not key.startswith(candidate):
                continue
            score = min(len(candidate), len(key))
            if best is None or score > best[0]:
                best = (score, spec)
    return best[1] if best else None


# Lines that are document titles or labels rather than a party's name.
_HEADER_STOPWORDS = (
    "gstin", "gst no", "invoice no", "invoice date", "billed to", "buyer", "place of supply",
    "po reference", "challan", "document number", "document date", "order no", "e-way bill",
    "vehicle", "valid upto", "total quantity", "received by", "supplier gstin", "state:",
)
_DOCUMENT_TITLES = {
    "tax invoice", "invoice", "bill of supply", "credit note", "debit note",
    "proforma invoice", "purchase order", "delivery challan", "goods receipt",
    "goods receipt note", "receipt note", "e-way bill", "statement of account",
    "account statement", "government of india", "tax invoice-cum-bill of supply",
}


def header_party_name(lines: list[str], limit: int = 8) -> tuple[str | None, int | None]:
    """Guess the issuing party from the document header.

    Invoices put the seller's name in the header block, in capitals, without a
    label: there is nothing to read as a ``label: value`` pair. This picks that
    line out and gives it a deliberately modest confidence, because a guess is a
    guess: the confidence router will send it to a reviewer on its own.
    """
    for index, line in enumerate(lines[:limit]):
        text = line.strip()
        if not 4 <= len(text) <= 90 or "|" in text or ":" in text:
            continue
        lowered = text.lower()
        if lowered in _DOCUMENT_TITLES:
            continue
        if any(stop in lowered for stop in _HEADER_STOPWORDS):
            continue
        letters = [c for c in text if c.isalpha()]
        if len(letters) < 6:
            continue
        if len(text.split()) < 2:
            continue
        if sum(1 for c in letters if c.isupper()) / len(letters) < 0.7:
            continue
        return text, index + 1
    return None, None


def _coerce(spec: FieldSpec, raw: Any) -> tuple[Any, float]:
    """Returns (value, confidence)."""
    if raw is None or (isinstance(raw, str) and raw.strip().lower() in UNREADABLE):
        return None, 0.15
    if spec.type == "decimal":
        value = clean_number(raw)
        return (value, 0.96) if value is not None else (None, 0.2)
    if spec.type == "int":
        value = clean_number(raw)
        return (int(value), 0.96) if value is not None else (None, 0.2)
    if spec.type == "date":
        value = clean_date(raw)
        return (value, 0.95) if value is not None else (None, 0.2)
    if spec.type == "gstin":
        value = clean_gstin(raw)
        return (value, 0.96) if value else (None, 0.2)
    if spec.type == "bool":
        text = str(raw).strip().lower()
        if text in {"yes", "y", "true"}:
            return True, 0.9
        if text in {"no", "n", "false"}:
            return False, 0.9
        return None, 0.2
    value = clean_text(raw)
    return (value, 0.94) if value is not None else (None, 0.25)


# ── tables ──────────────────────────────────────────────────────────────────


def detect_columns(line: str) -> list[str] | None:
    if "|" in line:
        return [c.strip() for c in line.split("|")]
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    if re.search(r"\s{3,}", line):
        return [c.strip() for c in re.split(r"\s{3,}", line.strip())]
    return None


def _resolve_header(header: str, item_specs: list[FieldSpec]) -> str | None:
    key = header.strip().lower()
    if key in TABLE_HEADER_ALIASES:
        mapped = TABLE_HEADER_ALIASES[key]
        if any(s.name == mapped for s in item_specs):
            return mapped
    spec = _match_field(header, item_specs)
    return spec.name if spec else None


def parse_table(
    lines: list[str], item_specs: list[FieldSpec]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Find a table and return (rows, row_confidences, header_line_number)."""
    for index, line in enumerate(lines):
        columns = detect_columns(line)
        if not columns or len(columns) < 2:
            continue
        mapping: dict[int, str] = {}
        for position, column in enumerate(columns):
            target = _resolve_header(column, item_specs)
            if target and target not in mapping.values():
                mapping[position] = target
        if len(mapping) < 2:
            continue

        rows: list[dict[str, Any]] = []
        confidences: list[dict[str, Any]] = []
        for row_line in lines[index + 1 :]:
            if not row_line.strip():
                if rows:
                    break
                continue
            cells = detect_columns(row_line)
            if not cells or len(cells) < 2:
                if rows:
                    break
                continue
            row: dict[str, Any] = {}
            row_conf: dict[str, Any] = {}
            for position, target in mapping.items():
                if position >= len(cells):
                    continue
                spec = next((s for s in item_specs if s.name == target), None)
                if spec is None:
                    continue
                value, conf = _coerce(spec, cells[position])
                if value is not None or conf < 0.5:
                    row[target] = value
                    row_conf[target] = conf
            if any(v is not None for v in row.values()):
                rows.append(row)
                confidences.append(row_conf)
        if rows:
            return rows, confidences, index + 1
    return [], [], 0


# ── extraction ──────────────────────────────────────────────────────────────


def extract_segment(segment: DocumentSegment, pack: Pack) -> list[ExtractedRecord]:
    """Extract one logical document (or, for a bank statement, one record per line)."""
    if not segment.doc_type or segment.doc_type == "unknown":
        return []
    spec = pack.document(segment.doc_type)
    lines = segment.text.splitlines()
    pairs = label_value_pairs(lines)
    scalar_specs = [f for f in spec.fields if f.type != "array"]
    array_specs = [f for f in spec.fields if f.type == "array"]

    fields: list[FieldValue] = []
    payload: dict[str, Any] = {}

    for label, raw_value, line_number in pairs:
        matched = _match_field(label, scalar_specs)
        if matched is None or matched.name in payload:
            continue
        value, confidence = _coerce(matched, raw_value)
        payload[matched.name] = value
        fields.append(
            FieldValue(
                path=matched.name,
                label=matched.label,
                value_text=None if value is None else str(value),
                value_num=value if isinstance(value, Decimal) else None,
                value_date=value if isinstance(value, dt.date) else None,
                confidence=confidence,
                page_no=segment.page_from,
                source_span=f"line {line_number}: {label.strip()}",
                mandatory=matched.mandatory,
            )
        )

    # The party name usually sits in the header block with no label at all.
    for name in ("vendor_name", "party_name"):
        name_spec = next((f for f in scalar_specs if f.name == name), None)
        if name_spec is None or payload.get(name):
            continue
        guess, line_number = header_party_name(lines)
        if guess:
            payload[name] = guess
            fields.append(
                FieldValue(
                    path=name,
                    label=name_spec.label,
                    value_text=guess,
                    confidence=0.72,
                    page_no=segment.page_from,
                    source_span=f"line {line_number}: document header",
                    mandatory=name_spec.mandatory,
                )
            )

    for array_spec in array_specs:
        rows, row_confidences, header_line = parse_table(lines, array_spec.item_fields)
        if not rows:
            continue
        payload[array_spec.name] = rows
        for row_index, (row, row_conf) in enumerate(zip(rows, row_confidences)):
            for key, value in row.items():
                item_spec = next((s for s in array_spec.item_fields if s.name == key), None)
                fields.append(
                    FieldValue(
                        path=f"{array_spec.name}.{row_index}.{key}",
                        label=item_spec.label if item_spec else key,
                        value_text=None if value is None else str(value),
                        value_num=value if isinstance(value, Decimal) else None,
                        confidence=float(row_conf.get(key, 0.5)),
                        page_no=segment.page_from,
                        source_span=f"line {header_line} table, row {row_index + 1}",
                        mandatory=bool(item_spec and item_spec.mandatory),
                    )
                )

    # Absent mandatory fields are recorded as low-confidence so the router sends
    # the record to review with a specific reason, rather than silently posting.
    for field_spec in spec.fields:
        if field_spec.mandatory and payload.get(field_spec.name) in (None, "", [], {}):
            if not any(f.path == field_spec.name for f in fields):
                fields.append(
                    FieldValue(
                        path=field_spec.name,
                        label=field_spec.label,
                        value_text=None,
                        confidence=0.0,
                        mandatory=True,
                        page_no=segment.page_from,
                        source_span="not found on the document",
                    )
                )

    payload.setdefault("_extraction", {"model": "baseline-text", "pack_version": pack.version})

    if spec.key == "bank_statement":
        return _bank_line_records(segment, spec, payload, fields, pack)

    record = ExtractedRecord(
        doc_type=spec.key,
        pack_key=pack.key,
        pack_version=pack.version,
        payload=payload,
        fields=fields,
        page_from=segment.page_from,
        page_to=segment.page_to,
        order_index=segment.order_index,
        model="baseline-text",
        prompt_version="baseline/v1",
        schema_ok=True,
    )
    return [record]


# The statement table is a line-per-transaction layout rather than an array field
# on the catalogue, so it is described here.
STATEMENT_LINE_FIELDS = [
    FieldSpec(name="date", label="Date", type="date", mandatory=True),
    FieldSpec(name="narration", label="Narration", type="string", mandatory=True),
    FieldSpec(name="reference", label="Reference number", type="string"),
    FieldSpec(name="debit", label="Debit", type="decimal"),
    FieldSpec(name="credit", label="Credit", type="decimal"),
    FieldSpec(name="balance", label="Balance", type="decimal"),
]


def _bank_line_records(
    segment: DocumentSegment,
    spec: DocumentTypeSpec,
    payload: dict[str, Any],
    fields: list[FieldValue],
    pack: Pack,
) -> list[ExtractedRecord]:
    """One statement, many lines: emit one record per movement (6.3)."""
    rows, row_confidences, _header_line = parse_table(
        segment.text.splitlines(), STATEMENT_LINE_FIELDS
    )
    account = payload.get("account_reference")
    records: list[ExtractedRecord] = []
    running_previous: Decimal | None = None
    order = 0
    for index, row in enumerate(rows):
        debit = row.get("debit")
        credit = row.get("credit")
        if debit is None and credit is None:
            balance = row.get("balance")
            if balance is not None:
                running_previous = balance
            continue
        line_payload: dict[str, Any] = {
            "account_reference": account,
            "date": row.get("date"),
            "narration": row.get("narration"),
            "debit": debit,
            "credit": credit,
            "balance": row.get("balance"),
            "reference": row.get("reference"),
            "_extraction": {"model": "baseline-text", "pack_version": pack.version},
        }
        if running_previous is not None:
            line_payload["_previous_balance"] = str(running_previous)
        if row.get("balance") is not None:
            running_previous = row["balance"]

        line_fields: list[FieldValue] = []
        for key, value in line_payload.items():
            if key.startswith("_") or value is None:
                # A withdrawal and a deposit are mutually exclusive by nature, so an
                # empty one is "not applicable", not a missing value. Treating it as
                # missing would drag the line's confidence down and push every
                # statement through the review queue.
                continue
            line_fields.append(
                FieldValue(
                    path=key,
                    label=key.replace("_", " ").capitalize(),
                    value_text=str(value),
                    value_num=value if isinstance(value, Decimal) else None,
                    value_date=value if isinstance(value, dt.date) else None,
                    confidence=0.95,
                    page_no=segment.page_from,
                    source_span=f"statement line {index + 1}",
                    mandatory=key in {"date", "narration"},
                )
            )
        for mandatory_key in ("date", "narration"):
            if line_payload.get(mandatory_key) is None:
                line_fields.append(
                    FieldValue(
                        path=mandatory_key,
                        label=mandatory_key.capitalize(),
                        value_text=None,
                        confidence=0.0,
                        mandatory=True,
                        page_no=segment.page_from,
                        source_span=f"statement line {index + 1}: not read",
                    )
                )
        records.append(
            ExtractedRecord(
                doc_type=spec.key,
                pack_key=pack.key,
                pack_version=pack.version,
                payload=line_payload,
                fields=line_fields,
                page_from=segment.page_from,
                page_to=segment.page_to,
                order_index=order,
                model="baseline-text",
                prompt_version="baseline/v1",
                schema_ok=True,
            )
        )
        order += 1
    return records
