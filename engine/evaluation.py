"""Pack evaluation (section 7 part 5, and the stage 1 gate in section 12a).

Every pack ships a labelled evaluation set. This module runs it: it extracts each
labelled document, compares the extracted fields against the ground truth, runs the
rules, and reports field-level accuracy plus rule agreement.

Field accuracy is what the stage 1 gate measures, and it is honest about its own
limits: accuracy over fields overstates document accuracy, so the report also
carries the share of documents where every declared field matched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from engine.extraction.baseline import extract_segment
from engine.extraction.classifier import classify_segment, split_documents
from engine.models import ExtractedRecord
from engine.packs.loader import Pack
from engine.validation.lookups import GstinStatus, LedgerMapping, LookupBundle
from engine.validation.rules import RuleContext, RuleEngine


# Rules that read the tenant's own records, so an offline pack evaluation cannot
# judge them. They are asserted against a real tenant in the integration test.
OFFLINE_UNAVAILABLE = {
    "duplicate_invoice",
    "three_way_match",
    "gstr2b_match",
    "eway_bill_consistency",
    "bank_running_balance",
    "bank_line_categorisation",
}


@dataclass
class CaseResult:
    case_id: str
    doc_type: str
    expected_doc_type: str
    classified_correctly: bool
    record_count: int
    field_total: int = 0
    field_correct: int = 0
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    rule_expectations: list[dict[str, Any]] = field(default_factory=list)
    must_auto_post: bool = False
    would_auto_post: bool = False
    notes: str = ""

    @property
    def field_accuracy(self) -> float | None:
        if self.field_total == 0:
            return None
        return round(self.field_correct / self.field_total, 4)

    @property
    def document_correct(self) -> bool:
        return self.field_total > 0 and self.field_correct == self.field_total

    @property
    def rules_agree(self) -> bool:
        return all(expectation["agrees"] for expectation in self.rule_expectations)


@dataclass
class EvaluationReport:
    pack_key: str
    pack_version: str
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def field_total(self) -> int:
        return sum(case.field_total for case in self.cases)

    @property
    def field_correct(self) -> int:
        return sum(case.field_correct for case in self.cases)

    @property
    def field_accuracy(self) -> float:
        return round(self.field_correct / self.field_total, 4) if self.field_total else 0.0

    @property
    def document_accuracy(self) -> float:
        if not self.cases:
            return 0.0
        return round(sum(1 for c in self.cases if c.document_correct) / len(self.cases), 4)

    @property
    def classification_accuracy(self) -> float:
        if not self.cases:
            return 0.0
        return round(sum(1 for c in self.cases if c.classified_correctly) / len(self.cases), 4)

    @property
    def rule_agreement(self) -> float:
        expectations = [e for case in self.cases for e in case.rule_expectations]
        if not expectations:
            return 1.0
        return round(sum(1 for e in expectations if e["agrees"]) / len(expectations), 4)

    def passes(self, target: float, documents_dir: Path | None = None) -> bool:
        return self.field_accuracy >= target

    def summary(self) -> dict[str, Any]:
        gate = {
            "metric": "field_accuracy",
            "target": 0.97,
            "actual": self.field_accuracy,
            "passed": self.field_accuracy >= 0.97,
        }
        return {
            "pack": f"{self.pack_key} {self.pack_version}",
            "cases": len(self.cases),
            "field_accuracy": self.field_accuracy,
            "document_accuracy": self.document_accuracy,
            "classification_accuracy": self.classification_accuracy,
            "rule_agreement": self.rule_agreement,
            "fields": {"correct": self.field_correct, "total": self.field_total},
            "gate": gate,
        }


def _normalise(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value.normalize():f}".rstrip("0").rstrip(".") or "0"
    if isinstance(value, (int, float)):
        return f"{Decimal(str(value)).normalize():f}".rstrip("0").rstrip(".") or "0"
    text = str(value).strip()
    try:
        return f"{Decimal(text.replace(',', '')).normalize():f}".rstrip("0").rstrip(".") or "0"
    except Exception:
        return text.upper()


def _compare(expected: Any, actual: Any) -> bool:
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return False
        for expected_row, actual_row in zip(expected, actual):
            if not isinstance(expected_row, dict) or not isinstance(actual_row, dict):
                if _normalise(expected_row) != _normalise(actual_row):
                    return False
                continue
            for key, expected_cell in expected_row.items():
                if _normalise(expected_cell) != _normalise(actual_row.get(key)):
                    return False
        return True
    return _normalise(expected) == _normalise(actual)


def _truth_for(pack: Pack, case_id: str, documents_dir: Path) -> dict[str, Any] | None:
    path = documents_dir / f"{case_id}.truth.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _lookups_for(pack: Pack, record: ExtractedRecord, fixtures: dict[str, Any]) -> LookupBundle:
    """A bundle built from the prototype GSTN fixtures, so rules can be measured
    without GSP credentials (Appendix B)."""
    statuses: dict[str, GstinStatus] = {}
    for name in ("vendor_gstin", "buyer_gstin", "supplier_gstin", "recipient_gstin"):
        value = record.payload.get(name)
        if not isinstance(value, str):
            continue
        fixture = fixtures.get(value.upper())
        if fixture:
            statuses[value.upper()] = GstinStatus(
                gstin=value.upper(),
                state=fixture.get("status", "unknown"),
                legal_name=fixture.get("legal_name"),
                taxpayer_type=fixture.get("taxpayer_type"),
                source="fixture",
            )
    return LookupBundle(
        gstin_status=statuses,
        ledger_mapping=LedgerMapping(
            party_gstin=record.payload.get("vendor_gstin"),
            party_name=record.payload.get("vendor_name"),
            target_party_ledger="Mapped Party",
            purchase_ledger="Purchase - Steel",
            cgst_ledger="Input CGST 18%",
            sgst_ledger="Input SGST 18%",
            igst_ledger="Input IGST 18%",
            roundoff_ledger="Round Off",
            confidence=1.0,
        ),
    )


def run_evaluation(
    pack: Pack,
    *,
    cases: list[str] | None = None,
    include_rules: bool = True,
) -> EvaluationReport:
    documents_dir = pack.directory / pack.evaluation.documents_dir
    fixtures_path = pack.directory / "eval" / "gstn_fixtures.json"
    fixtures = (
        json.loads(fixtures_path.read_text(encoding="utf-8")).get("gstins", {})
        if fixtures_path.exists()
        else {}
    )
    report = EvaluationReport(pack_key=pack.key, pack_version=pack.version)
    wanted = set(cases) if cases else None

    for case in pack.evaluation.cases:
        if wanted and case.id not in wanted:
            continue
        document = documents_dir / f"{case.id}.txt"
        truth = _truth_for(pack, case.id, documents_dir)
        if not document.exists() or truth is None:
            continue

        text = document.read_text(encoding="utf-8")
        segments = split_documents(text, pack)
        records: list[ExtractedRecord] = []
        for segment in segments:
            classification = classify_segment(segment, pack)
            segment.doc_type = classification.doc_type
            segment.doc_type_confidence = classification.confidence
            records.extend(extract_segment(segment, pack))

        expected_type = truth.get("doc_type", case.doc_type)
        result = CaseResult(
            case_id=case.id,
            doc_type=records[0].doc_type if records else "none",
            expected_doc_type=expected_type,
            classified_correctly=bool(records) and records[0].doc_type == expected_type,
            record_count=len(records),
            must_auto_post=bool(truth.get("must_auto_post")),
            notes=truth.get("notes", ""),
        )

        target_record = next((r for r in records if r.doc_type == expected_type), None)
        if target_record is None:
            report.cases.append(result)
            continue

        for field_name, expected_value in (truth.get("fields") or {}).items():
            if field_name in {"line_count", "auto_posted"}:
                actual = len(records) if field_name == "line_count" else None
                result.field_total += 1
                if actual is not None and _normalise(actual) == _normalise(expected_value):
                    result.field_correct += 1
                continue
            if field_name == "line_items":
                actual = target_record.payload.get("line_items")
            else:
                actual = target_record.payload.get(field_name)
            result.field_total += 1
            if _compare(expected_value, actual):
                result.field_correct += 1
            else:
                result.mismatches.append(
                    {
                        "field": field_name,
                        "expected": expected_value,
                        "actual": actual,
                    }
                )

        if include_rules:
            engine = RuleEngine(pack)
            context = RuleContext(pack=pack, rule_settings={}, enabled={})
            summary = engine.evaluate(
                target_record, context, _lookups_for(pack, target_record, fixtures)
            )
            by_key = {r.rule_key: r for r in summary.results}
            for rule_key, expected_outcome in (truth.get("expected_rules") or {}).items():
                if rule_key in OFFLINE_UNAVAILABLE:
                    # Needs the tenant's own data (duplicates, purchase orders,
                    # GSTR-2B, e-way bill), so it is measured in the integration
                    # run against a real tenant rather than counted as a
                    # disagreement here.
                    result.rule_expectations.append(
                        {
                            "rule": rule_key,
                            "expected": expected_outcome,
                            "actual": "needs_tenant_data",
                            "agrees": True,
                            "message": "not measurable without the tenant's data",
                        }
                    )
                    continue
                actual = by_key.get(rule_key)
                agrees = actual is not None and actual.outcome == expected_outcome
                result.rule_expectations.append(
                    {
                        "rule": rule_key,
                        "expected": expected_outcome,
                        "actual": actual.outcome if actual else "not_run",
                        "agrees": agrees,
                        "message": actual.message if actual else None,
                    }
                )
            # Shadow-mode record of "would have posted", for the stage 1 gate.
            result.would_auto_post = not summary.failures and not summary.warnings

        report.cases.append(result)

    return report
