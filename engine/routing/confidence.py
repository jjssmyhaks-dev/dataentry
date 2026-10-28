"""The confidence router (section 6.4).

Decides whether a validated record may post untouched, or goes to a reviewer.
Conservative by default, and every threshold is a setting, because section 12a
leaves the gate numbers open until a design partner and their CA agree them.

The decision is a pure function of the record, the rule results and the tenant's
own thresholds, so it never depends on anything the browser sends.

Three posting modes (Appendix C):

* ``shadow`` (autonomy stage 1) — nothing is posted. A record that would have
  posted is recorded as such, because that is exactly what the stage 1 gate
  measures.
* ``draft`` — the voucher is staged and waits for approval in the review queue.
* ``autonomous`` — stage 2 and above post directly, subject to the safety limits.
"""

from __future__ import annotations

from decimal import Decimal

from engine.models import (
    RoutingDecision,
    RoutingInput,
    RoutingReason,
    ValidationSummary,
)

# Rules whose failure is never worth a "just double check it" decision: they mean
# the record itself is wrong, duplicated, or outside a posting period.
CRITICAL_RULES = (
    "gstin_format",
    "gstin_active_status",
    "tax_type_vs_place_of_supply",
    "total_arithmetic",
    "duplicate_invoice",
    "date_sanity",
)

# Stage 2 (autonomous on low-risk) needs a vendor we have posted for before and a
# learned ledger mapping we trust.
STAGE_2_MIN_MAPPING_CONFIDENCE = 0.95


def decide(inp: RoutingInput) -> RoutingDecision:
    record = inp.record
    validation: ValidationSummary = inp.validation
    blocking: list[RoutingReason] = []

    # ── 1. Can this record be worked at all? ────────────────────────────────
    if not record.schema_ok:
        return RoutingDecision(
            decision="exception",
            reasons=[
                RoutingReason(
                    code="schema_failed",
                    message=(
                        "The document could not be read into the pack's schema. "
                        "A person needs to look at the file."
                    ),
                )
            ],
            priority=5,
            priority_band="rule_failure",
        )

    # ── 2. Rule failures block auto-post outright ───────────────────────────
    for result in validation.failures:
        blocking.append(
            RoutingReason(
                code=f"rule_failed:{result.rule_key}",
                message=result.message or f"{result.title} failed.",
                fields=result.fields,
                rule_key=result.rule_key,
            )
        )

    # ── 3. Warnings block auto-post unless the tenant allows them ───────────
    if not inp.warns_allow_autopost:
        for result in validation.warnings:
            blocking.append(
                RoutingReason(
                    code=f"rule_warned:{result.rule_key}",
                    message=result.message or f"{result.title} needs a look.",
                    fields=result.fields,
                    rule_key=result.rule_key,
                )
            )
    for result in validation.degraded:
        blocking.append(
            RoutingReason(
                code=f"rule_degraded:{result.rule_key}",
                message=(
                    f"{result.title} could not be checked right now; the record will "
                    "be rechecked."
                ),
                rule_key=result.rule_key,
            )
        )

    # ── 4. Confidence ───────────────────────────────────────────────────────
    threshold = Decimal(str(inp.confidence_threshold))
    if Decimal(str(record.overall_confidence)) < threshold:
        blocking.append(
            RoutingReason(
                code="low_overall_confidence",
                message=(
                    f"Overall confidence {record.overall_confidence:.0%} is below the "
                    f"{float(threshold):.0%} this tenant requires."
                ),
            )
        )
    low_fields = record.low_confidence(float(threshold), mandatory_only=True)
    if low_fields:
        blocking.append(
            RoutingReason(
                code="low_field_confidence",
                message="These required fields were read with low confidence: "
                + ", ".join(low_fields),
                fields=low_fields,
            )
        )

    # ── 5. Duplicates ───────────────────────────────────────────────────────
    if inp.duplicate_exact and not any(r.rule_key == "duplicate_invoice" for r in blocking):
        blocking.append(
            RoutingReason(
                code="duplicate_exact",
                message="This looks like a document already recorded.",
                rule_key="duplicate_invoice",
            )
        )

    # ── 6. Autonomy stage and safety limits ─────────────────────────────────
    mode = inp.posting_mode
    stage = inp.autonomy_stage
    value = inp.document_value
    cap = inp.autopost_value_cap
    mode_notes: list[RoutingReason] = []

    if mode == "shadow":
        mode_notes.append(
            RoutingReason(
                code="mode_shadow",
                message=(
                    "Shadow mode: nothing is posted. This record is kept only to "
                    "measure what the engine would have posted."
                ),
            )
        )
    elif mode == "draft":
        mode_notes.append(
            RoutingReason(
                code="mode_draft",
                message="This tenant is in draft mode, so the voucher is staged for approval.",
            )
        )
    elif stage < 2:
        blocking.append(
            RoutingReason(
                code="stage_below_autonomous",
                message=(
                    f"The tenant is at autonomy stage {stage}; only stage 2 and above "
                    "may post without a person."
                ),
            )
        )

    if mode == "autonomous" and stage >= 2:
        if stage == 2:
            if not inp.is_known_vendor:
                blocking.append(
                    RoutingReason(
                        code="unknown_vendor",
                        message=(
                            "Stage 2 posts only for vendors already mapped to a ledger; "
                            "this vendor is new."
                        ),
                    )
                )
            mapping_confidence = inp.ledger_mapping_confidence
            if mapping_confidence is None or mapping_confidence < STAGE_2_MIN_MAPPING_CONFIDENCE:
                blocking.append(
                    RoutingReason(
                        code="ledger_mapping_untrusted",
                        message=(
                            "There is no ledger mapping for this vendor that the engine "
                            "trusts well enough for stage 2."
                        ),
                    )
                )
        if cap is not None and value is not None and value > cap:
            blocking.append(
                RoutingReason(
                    code="above_value_cap",
                    message=(
                        f"Voucher value {value} is above this tenant's automatic posting "
                        f"limit of {cap}."
                    ),
                )
            )
        if inp.daily_autopost_cap is not None and inp.daily_autopost_used >= inp.daily_autopost_cap:
            blocking.append(
                RoutingReason(
                    code="daily_cap_reached",
                    message=(
                        f"The daily automatic posting cap of {inp.daily_autopost_cap} "
                        "vouchers has been reached."
                    ),
                )
            )
        blocked_rules = {
            r.rule_key for r in blocking if r.rule_key and r.rule_key in CRITICAL_RULES
        }
        if blocked_rules:
            blocking.append(
                RoutingReason(
                    code="critical_rule_failed",
                    message=(
                        "A safety-critical check failed: "
                        + ", ".join(sorted(blocked_rules))
                        + ". This record cannot post on its own."
                    ),
                )
            )

    # ── 7. Decision ─────────────────────────────────────────────────────────
    band = "rule_failure" if validation.failures else "low_confidence"
    priority = _priority(band, value, inp)

    if mode == "shadow":
        # Stage 1: record what would have been posted; post nothing either way.
        if not blocking:
            return RoutingDecision(
                decision="auto_post",
                posting_mode="shadow",
                reasons=mode_notes
                + [
                    RoutingReason(
                        code="would_have_posted",
                        message=(
                            "Every rule passed and every field cleared its confidence "
                            "threshold, so this record would have posted untouched."
                        ),
                    )
                ],
                priority=95,
                priority_band="routine",
            )
        return RoutingDecision(
            decision="review",
            posting_mode="shadow",
            reasons=mode_notes + blocking,
            priority=priority,
            priority_band=band,
            requires_supervisor=_requires_supervisor(inp, value),
        )

    if mode == "autonomous" and stage >= 2 and not blocking:
        return RoutingDecision(
            decision="auto_post",
            posting_mode="autonomous",
            reasons=[
                RoutingReason(
                    code="all_checks_passed",
                    message="Every rule passed and every field cleared its confidence threshold.",
                )
            ],
            priority=90,
            priority_band="routine",
        )

    return RoutingDecision(
        decision="review",
        posting_mode="draft",
        reasons=mode_notes + blocking,
        priority=priority,
        priority_band=band,
        requires_supervisor=_requires_supervisor(inp, value),
    )


def _priority(band: str, value: Decimal | None, inp: RoutingInput) -> int:
    """Lower runs sooner.

    Default order is rule failures first, then low confidence, then oldest first,
    with high-value records ahead of low-value ones (Appendix D).
    """
    base = {"rule_failure": 10, "low_confidence": 50, "routine": 100}[band]

    if value is not None:
        if value >= Decimal("500000"):
            base -= 4
        elif value >= Decimal("100000"):
            base -= 2
        elif value < Decimal("5000"):
            base += 20

    if inp.due_soon:
        base -= 5
    return max(base, 1)


def _requires_supervisor(inp: RoutingInput, value: Decimal | None) -> bool:
    """Approval rights can be limited by amount (Appendix D)."""
    if inp.approval_limit is None or value is None:
        return False
    return value > inp.approval_limit
