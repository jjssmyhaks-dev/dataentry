"""Classification and splitting (section 6.1 and Appendix A).

* Classification picks the document type using the tenant's active packs.
* Splitting handles a file that holds several documents — a bundle of invoices
  in one PDF is split before extraction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from engine.packs.loader import Pack

# Phrases that reliably open a new logical document inside one file.
DOCUMENT_START_MARKERS: dict[str, list[str]] = {
    "supplier_invoice": ["tax invoice", "invoice", "bill of supply", "credit note", "debit note"],
    "purchase_order": ["purchase order", "order confirmation"],
    "goods_receipt": ["delivery challan", "goods receipt", "receipt note", "grn"],
    "e_way_bill": ["e-way bill", "eway bill", "e way bill"],
    "e_invoice": ["e-invoice", "invoice reference number"],
    "bank_statement": ["statement of account", "account statement", "bank statement"],
}

# Markers strong enough to start a split on their own. A bare "invoice" is not,
# because it appears in the body of almost every document.
STRONG_START_MARKERS = [
    "tax invoice",
    "tax invoice-cum-bill of supply",
    "purchase order",
    "delivery challan",
    "goods receipt note",
    "e-way bill",
    "statement of account",
    "credit note",
    "debit note",
    "proforma invoice",
]

NOISE_MARKERS = ["continued", "page 2", "annexure"]


@dataclass
class ClassificationResult:
    doc_type: str
    confidence: float
    scores: dict[str, float] = field(default_factory=dict)
    method: str = "hints"


@dataclass
class DocumentSegment:
    text: str
    doc_type: str | None = None
    doc_type_confidence: float | None = None
    page_from: int = 1
    page_to: int = 1
    order_index: int = 0
    split_reason: str | None = None


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


def classify(text: str, pack: Pack) -> ClassificationResult:
    """Score the text against each document type's classify hints."""
    haystack = _normalise(text)
    scores: dict[str, float] = {}
    for doc_type, hints in pack.classification_table():
        score = 0.0
        for hint in hints:
            needle = _normalise(hint)
            if needle and needle in haystack:
                # Longer, more specific hints are worth more.
                score += 1.0 + min(len(needle) / 40.0, 1.0)
        scores[doc_type] = round(score, 4)

    if not scores or max(scores.values()) <= 0:
        return ClassificationResult(doc_type="unknown", confidence=0.0, scores=scores, method="hints")

    best_type = max(scores, key=lambda k: scores[k])
    total = sum(scores.values())
    confidence = round(scores[best_type] / total, 4) if total else 0.0
    return ClassificationResult(
        doc_type=best_type,
        confidence=confidence,
        scores=scores,
        method="hints",
    )


def _starts_new_document(line: str) -> tuple[bool, str | None]:
    stripped = line.strip().strip(":-").lower()
    if not stripped:
        return False, None
    if len(stripped) > 60:
        return False, None
    if any(noise in stripped for noise in NOISE_MARKERS):
        return False, None
    for marker in STRONG_START_MARKERS:
        if stripped == marker or stripped.startswith(marker):
            doc_type = _type_for_marker(marker)
            return True, doc_type
    return False, None


def _type_for_marker(marker: str) -> str:
    for doc_type, markers in DOCUMENT_START_MARKERS.items():
        if any(marker.startswith(m) for m in markers):
            return doc_type
    return "unknown"


def split_documents(text: str, pack: Pack, *, expect: str | None = None) -> list[DocumentSegment]:
    """Split one file's text into logical documents.

    A single sheet of paper is not a container, so the common case returns one
    segment. Only a clear start marker opens a new segment.
    """
    lines = text.splitlines()
    segments: list[tuple[str | None, list[str]]] = []
    current_type: str | None = None
    current: list[str] = []
    split_count = 0

    for line in lines:
        is_start, doc_type = _starts_new_document(line)
        if is_start and current and any(l.strip() for l in current):
            # Only split when the line genuinely opens a new document: either the
            # segment is already long enough to be one, or the declared types differ.
            if len(current) >= 4 or _is_terminal(current):
                segments.append((current_type, current))
                current, split_count = [], split_count + 1
                current_type = doc_type
                current.append(line)
                continue
        if is_start and not current:
            current_type = doc_type
        current.append(line)

    if any(l.strip() for l in current):
        segments.append((current_type, current))

    if not segments:
        segments = [(None, [text])]

    result: list[DocumentSegment] = []
    for index, (doc_type, seg_lines) in enumerate(segments):
        seg_text = "\n".join(seg_lines).strip()
        if not seg_text:
            continue
        if doc_type is None:
            classification = classify(seg_text, pack)
            doc_type, confidence = classification.doc_type, classification.confidence
        else:
            confidence = 0.97
        result.append(
            DocumentSegment(
                text=seg_text,
                doc_type=doc_type,
                doc_type_confidence=confidence,
                order_index=index,
                split_reason="start_marker" if split_count else None,
            )
        )

    if expect and len(result) == 1 and not result[0].doc_type:
        result[0].doc_type = expect
    return result


def _is_terminal(lines: list[str]) -> bool:
    """A segment that ends with a totals or signature block is a finished document."""
    tail = _normalise(" ".join(lines[-6:]))
    return any(
        marker in tail
        for marker in ("grand total", "amount in words", "authorised signatory", "for ", "e. & o.e")
    )


def classify_segment(segment: DocumentSegment, pack: Pack) -> ClassificationResult:
    if segment.doc_type:
        return ClassificationResult(
            doc_type=segment.doc_type,
            confidence=segment.doc_type_confidence or 0.9,
            method="marker",
        )
    return classify(segment.text, pack)


def summarise_split(segments: list[DocumentSegment]) -> list[dict[str, Any]]:
    return [
        {
            "order_index": s.order_index,
            "doc_type": s.doc_type,
            "confidence": s.doc_type_confidence,
            "pages": [s.page_from, s.page_to],
            "split_reason": s.split_reason,
            "chars": len(s.text),
        }
        for s in segments
    ]
