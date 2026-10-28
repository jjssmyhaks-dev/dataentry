"""The rules engine (section 6.3, Appendix B).

Reads the pack's declarative rules, resolves per-tenant thresholds and
tolerances, dispatches each rule to its named check, and records a pass, warn or
fail for every one. Every result is kept with the record so a reviewer sees
exactly why a document was flagged.

Severity contract:

* ``rule.severity`` is the highest severity the rule may produce. A ``warn`` rule
  can never block auto-post, however badly it fails.
* A rule that needs an external lookup degrades to its ``degrade_to`` outcome when
  that lookup is unavailable, and is marked ``degraded`` so the record can be
  rechecked later instead of being blocked.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from engine.models import ExtractedRecord, RuleResult, ValidationSummary
from engine.packs.loader import Pack
from engine.packs.models import RuleDefinition
from engine.validation import checks as checks_mod
from engine.validation.lookups import LookupBundle

_SEVERITY_RANK = {"info": 0, "warn": 1, "fail": 2}


@dataclass
class RuleContext:
    """Per-tenant configuration for a validation run."""

    pack: Pack
    tenant_id: UUID | None = None
    today: dt.date = field(default_factory=dt.date.today)
    # rule_key -> merged settings (pack defaults overridden by tenant)
    rule_settings: dict[str, dict[str, Any]] = field(default_factory=dict)
    enabled: dict[str, bool] = field(default_factory=dict)
    severity_overrides: dict[str, str] = field(default_factory=dict)
    # Tenant-level flags the compliance rules read.
    e_invoicing_applies: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class RuleEngine:
    def __init__(self, pack: Pack) -> None:
        self.pack = pack

    # -- configuration ------------------------------------------------------

    def resolve_settings(self, rule: RuleDefinition, ctx: RuleContext) -> dict[str, Any]:
        merged = dict(rule.settings)
        merged.update(ctx.rule_settings.get(rule.key, {}))
        if rule.requires_lookup == "gstn":
            merged["tenant_e_invoicing_applies"] = ctx.e_invoicing_applies
        merged["tenant_e_invoicing_applies"] = ctx.e_invoicing_applies
        return merged

    def resolve_severity(self, rule: RuleDefinition, ctx: RuleContext) -> str:
        override = ctx.severity_overrides.get(rule.key)
        if override in _SEVERITY_RANK:
            return override
        return rule.severity

    # -- evaluation ---------------------------------------------------------

    def evaluate(
        self,
        record: ExtractedRecord,
        ctx: RuleContext | None = None,
        lookups: LookupBundle | None = None,
    ) -> ValidationSummary:
        ctx = ctx or RuleContext(pack=self.pack)
        lookups = lookups or LookupBundle()
        results: list[RuleResult] = []

        for rule in self.pack.rules_for(record.doc_type):
            results.append(self._evaluate_rule(rule, record, ctx, lookups))

        return ValidationSummary(results=results)

    def _evaluate_rule(
        self,
        rule: RuleDefinition,
        record: ExtractedRecord,
        ctx: RuleContext,
        lookups: LookupBundle,
    ) -> RuleResult:
        base = RuleResult(
            rule_key=rule.key,
            rule_version=rule.version,
            severity=self.resolve_severity(rule, ctx),  # type: ignore[arg-type]
            outcome="skipped",
            title=rule.title,
        )

        if ctx.enabled.get(rule.key) is False:
            return base.model_copy(update={"message": "Rule is switched off for this tenant."})

        check = checks_mod.get_check(rule.check)
        if check is None:
            return base.model_copy(
                update={
                    "outcome": "warn",
                    "message": (
                        f"Rule {rule.key} names a check ({rule.check}) this engine version "
                        "does not implement; the pack and engine versions may not match."
                    ),
                    "degraded": True,
                }
            )

        outcome = check(
            checks_mod.CheckInput(
                record=record,
                rule=rule,
                settings=self.resolve_settings(rule, ctx),
                lookups=lookups,
                pack=self.pack,
                today=ctx.today,
            )
        )

        final = self._cap(outcome.outcome, base.severity)
        message = outcome.message

        # A lookup-dependent rule that could not reach its source degrades
        # instead of failing, and is marked for a later recheck.
        degraded = outcome.degraded
        if rule.requires_lookup and (degraded or self._lookup_unavailable(rule, record, lookups)):
            degraded = True
            final = self._cap(rule.degrade_to if rule.degrade_to != "skip" else "skipped", base.severity)
            if message is None:
                message = (
                    f"{rule.title} could not be checked because the "
                    f"{rule.requires_lookup} lookup is unavailable; the record will be rechecked."
                )

        return base.model_copy(
            update={
                "outcome": final,
                "message": message,
                "details": outcome.details,
                "fields": outcome.fields or rule.fields[:1],
                "degraded": degraded,
                "recheck_at": (
                    dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=6) if degraded else None
                ),
            }
        )

    @staticmethod
    def _lookup_unavailable(rule: RuleDefinition, record: ExtractedRecord, lookups: LookupBundle) -> bool:
        if rule.requires_lookup == "gstn":
            if rule.check == "gstr2b_match":
                return lookups.gstr2b is None
            gstins = checks_mod.present_gstins(record, rule.fields)
            if not gstins:
                return False
            return any(
                (status := lookups.status_of(value)) is None or status.degraded
                for value in gstins.values()
            )
        return False

    @staticmethod
    def _cap(outcome: str, severity: str) -> str:
        if outcome in {"pass", "skipped"}:
            return outcome
        if _SEVERITY_RANK.get(outcome, 1) > _SEVERITY_RANK.get(severity, 1):
            return severity
        return outcome


def summarise(summary: ValidationSummary) -> dict[str, Any]:
    """A compact form for the review queue and the dashboards."""
    return {
        "failed": summary.failed_keys,
        "warned": summary.warned_keys,
        "degraded": [r.rule_key for r in summary.degraded],
        "passed": [r.rule_key for r in summary.results if r.outcome == "pass"],
        "total": len(summary.results),
    }


__all__ = ["RuleContext", "RuleEngine", "LookupBundle", "RuleResult", "ValidationSummary", "summarise"]
