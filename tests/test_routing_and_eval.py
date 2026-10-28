"""The confidence router, the autonomy gates, and the pack evaluation harness."""

from __future__ import annotations

from decimal import Decimal

import pytest

from engine.evaluation import run_evaluation
from engine.models import (
    ExtractedRecord,
    FieldValue,
    RuleResult,
    RoutingInput,
    ValidationSummary,
)
from engine.routing.autonomy import (
    AutonomyMetrics,
    describe_stage,
    evaluate_gate,
    should_auto_demote,
)
from engine.routing.confidence import decide


def _record(payload: dict | None = None, confidence: float = 0.98) -> ExtractedRecord:
    return ExtractedRecord(
        doc_type="supplier_invoice",
        pack_key="manufacturing",
        pack_version="1.0.0",
        payload=payload or {"total": "1000.00"},
        fields=[
            FieldValue(path="vendor_gstin", value_text="27AAPFU0939F1ZV", confidence=confidence, mandatory=True),
            FieldValue(path="total", value_num=Decimal("1000.00"), confidence=confidence, mandatory=True),
        ],
    )


def _input(**overrides) -> RoutingInput:
    base = dict(
        record=_record(),
        validation=ValidationSummary(),
        confidence_threshold=0.93,
        posting_mode="autonomous",
        autonomy_stage=3,
        document_value=Decimal("1000.00"),
        autopost_value_cap=Decimal("50000"),
    )
    base.update(overrides)
    return RoutingInput(**base)


# ── the router ──────────────────────────────────────────────────────────────


def test_clean_record_posts_when_the_tenant_is_autonomous():
    decision = decide(_input())
    assert decision.decision == "auto_post"
    assert decision.posting_mode == "autonomous"


def test_shadow_mode_records_what_it_would_have_posted_and_posts_nothing():
    decision = decide(_input(posting_mode="shadow", autonomy_stage=1))
    assert decision.decision == "auto_post"
    assert decision.posting_mode == "shadow"
    assert any(r.code == "would_have_posted" for r in decision.reasons)


def test_shadow_mode_still_routes_a_broken_record_to_review():
    summary = ValidationSummary(
        results=[RuleResult(rule_key="total_arithmetic", outcome="fail", message="differs by 12")]
    )
    decision = decide(_input(validation=summary, posting_mode="shadow", autonomy_stage=1))
    assert decision.decision == "review"
    assert decision.posting_mode == "shadow"
    assert decision.priority_band == "rule_failure"


def test_a_failed_rule_blocks_auto_post_at_every_stage():
    summary = ValidationSummary(
        results=[RuleResult(rule_key="duplicate_invoice", outcome="fail", message="already recorded")]
    )
    decision = decide(_input(validation=summary, autonomy_stage=4))
    assert decision.decision == "review"
    assert any(r.code == "critical_rule_failed" for r in decision.reasons)


def test_a_warning_blocks_unless_the_tenant_allows_warnings():
    summary = ValidationSummary(
        results=[RuleResult(rule_key="tds_applicability", outcome="warn", message="TDS may apply")]
    )
    assert decide(_input(validation=summary)).decision == "review"
    assert decide(_input(validation=summary, warns_allow_autopost=True)).decision == "auto_post"


def test_a_degraded_lookup_routes_to_review_rather_than_failing_silently():
    summary = ValidationSummary(
        results=[
            RuleResult(
                rule_key="gstin_active_status",
                outcome="warn",
                degraded=True,
                message="GSTN lookup unavailable",
            )
        ]
    )
    decision = decide(_input(validation=summary))
    assert decision.decision == "review"
    assert any(r.code == "rule_degraded:gstin_active_status" for r in decision.reasons)


def test_low_field_confidence_names_the_fields():
    decision = decide(_input(record=_record(confidence=0.55)))
    assert decision.decision == "review"
    reason = next(r for r in decision.reasons if r.code == "low_field_confidence")
    assert "vendor_gstin" in reason.fields


def test_the_value_cap_holds_higher_value_records_back():
    decision = decide(_input(document_value=Decimal("90000.00"), autopost_value_cap=Decimal("50000")))
    assert decision.decision == "review"
    assert any(r.code == "above_value_cap" for r in decision.reasons)


def test_the_daily_cap_stops_automatic_posting():
    decision = decide(_input(daily_autopost_used=200, daily_autopost_cap=200))
    assert any(r.code == "daily_cap_reached" for r in decision.reasons)


def test_stage_two_needs_a_known_vendor_and_a_trusted_mapping():
    unknown = decide(
        _input(autonomy_stage=2, is_known_vendor=False, ledger_mapping_confidence=0.99)
    )
    assert any(r.code == "unknown_vendor" for r in unknown.reasons)

    untrusted = decide(
        _input(autonomy_stage=2, is_known_vendor=True, ledger_mapping_confidence=0.6)
    )
    assert any(r.code == "ledger_mapping_untrusted" for r in untrusted.reasons)

    ready = decide(_input(autonomy_stage=2, is_known_vendor=True, ledger_mapping_confidence=0.99))
    assert ready.decision == "auto_post"


def test_stage_one_or_below_never_posts_on_its_own():
    decision = decide(_input(posting_mode="draft", autonomy_stage=1))
    assert decision.decision == "review"
    assert any(r.code == "mode_draft" for r in decision.reasons)


def test_an_unreadable_document_is_an_exception_not_a_review():
    record = _record()
    record.schema_ok = False
    decision = decide(_input(record=record))
    assert decision.decision == "exception"


def test_priority_puts_rule_failures_first_and_lifts_high_values():
    summary = ValidationSummary(
        results=[RuleResult(rule_key="total_arithmetic", outcome="fail", message="x")]
    )
    failing = decide(_input(validation=summary, document_value=Decimal("600000")))
    low_confidence = decide(_input(record=_record(confidence=0.5)))
    assert failing.priority < low_confidence.priority


def test_approval_limit_marks_a_record_for_a_supervisor():
    decision = decide(_input(document_value=Decimal("300000"), approval_limit=Decimal("250000")))
    assert decision.requires_supervisor


# ── autonomy gates ──────────────────────────────────────────────────────────


def test_stage_two_gate_measures_the_error_rate_not_field_accuracy():
    healthy = AutonomyMetrics(auto_posted=1000, auto_posted_corrected=2, observation_days=45)
    result = evaluate_gate(2, healthy)
    assert result.passed

    breaching = AutonomyMetrics(auto_posted=100, unreversed_errors=2, observation_days=45)
    result = evaluate_gate(2, breaching)
    assert not result.passed
    assert any(c.key == "error_rate" for c in result.blocking)


def test_stage_one_gate_needs_shadow_documents_and_no_unexplained_differences():
    thin = AutonomyMetrics(field_accuracy=0.99, shadow_documents=10)
    assert not evaluate_gate(1, thin).passed

    ready = AutonomyMetrics(field_accuracy=0.98, shadow_documents=150, shadow_discrepancies=0)
    assert evaluate_gate(1, ready).passed


def test_a_gate_with_no_definitions_has_nothing_blocking():
    result = evaluate_gate(4, AutonomyMetrics())
    assert result.passed is False or result.checks == []


def test_automatic_demotion_triggers_on_any_unreversed_error():
    demote, reason = should_auto_demote(
        error_limit=Decimal("0.005"),
        metrics=AutonomyMetrics(auto_posted=500, unreversed_errors=1),
    )
    assert demote
    assert "unreversed" in reason


def test_automatic_demotion_respects_the_tenant_setting():
    demote, reason = should_auto_demote(
        error_limit=Decimal("0.005"),
        metrics=AutonomyMetrics(auto_posted=500, unreversed_errors=1),
        auto_demote_enabled=False,
    )
    assert not demote


def test_stage_descriptions_are_available_for_the_admin_screen():
    described = describe_stage(2)
    assert described["name"] == "Autonomous on low-risk"
    assert described["gate_to_next"]


# ── the pack evaluation harness ─────────────────────────────────────────────


def test_evaluation_set_reaches_the_stage_one_gate(pack):
    report = run_evaluation(pack)
    summary = report.summary()
    assert summary["cases"] >= 10
    assert summary["classification_accuracy"] == 1.0
    assert summary["rule_agreement"] == 1.0, [
        e for case in report.cases for e in case.rule_expectations if not e["agrees"]
    ]
    assert summary["field_accuracy"] >= pack.evaluation.gate["target"], summary


def test_evaluation_reports_its_own_source_as_synthetic(pack):
    """The gate needs real design-partner documents; until they exist the pack says
    so rather than implying the number is meaningful."""
    assert pack.evaluation.source == "synthetic"
    assert "NOT the design-partner documents" in pack.evaluation.source_caveat


@pytest.mark.parametrize(
    "case_id",
    ["inv-003-tax-mismatch", "inv-004-total-mismatch", "inv-006-future-date"],
)
def test_labelled_failure_cases_must_not_auto_post(pack, case_id):
    report = run_evaluation(pack, cases=[case_id])
    case = report.cases[0]
    assert case.must_auto_post is False
    assert case.would_auto_post is False
