"""The connector interface (section 6.5).

The interface is common across every destination: validate the payload, post it,
confirm it landed, and be able to roll it back. TallyPrime, CSV/Excel and the
webhook all implement it, so adding a destination does not change the pipeline.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from engine.db import TenantRef
from engine.models import ExtractedRecord, VoucherDraft


@dataclass
class PostContext:
    tenant: TenantRef
    record: ExtractedRecord
    record_row: dict[str, Any]
    draft: VoucherDraft
    voucher_id: uuid.UUID | None = None
    agent: dict[str, Any] | None = None
    mode: str = "draft"


@dataclass
class PostResult:
    ok: bool
    status: str
    connector: str
    response: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_text: str | None = None
    require_human: bool = False
    retry_after_seconds: int | None = None

    @property
    def tally_voucher_number(self) -> str | None:
        return self.response.get("voucher_number")

    @property
    def tally_voucher_id(self) -> str | None:
        return self.response.get("voucher_id")


class Connector(Protocol):
    name: str
    label: str

    async def validate(self, ctx: PostContext) -> list[str]:
        """Problems that must be fixed before this voucher can be sent."""

    async def post(self, ctx: PostContext) -> PostResult:
        """Idempotent: the same voucher must never be created twice."""

    async def confirm(self, ctx: PostContext, result: PostResult) -> PostResult:
        """Read back what the destination actually recorded."""

    async def roll_back(self, ctx: PostContext, result: PostResult) -> PostResult:
        """Undo a posted voucher, within an open financial period."""


MISSING_SHAPE_ERRORS = (
    "the voucher has no lines",
    "the voucher does not balance",
)


def basic_validation(ctx: PostContext) -> list[str]:
    problems: list[str] = []
    if not ctx.draft.lines:
        problems.append("The voucher has no lines, so there is nothing to post.")
    elif not ctx.draft.totals_balanced():
        debit = sum((line.amount for line in ctx.draft.lines if line.is_debit), 0)
        credit = sum((line.amount for line in ctx.draft.lines if not line.is_debit), 0)
        problems.append(
            f"The voucher does not balance: debits {debit} against credits {credit}."
        )
    if not ctx.draft.voucher_type:
        problems.append("The voucher type is not known.")
    unnamed = [line for line in ctx.draft.lines if not (line.ledger or line.stock_item)]
    if unnamed:
        problems.append(
            f"{len(unnamed)} voucher line(s) have no ledger or stock item mapped. "
            "Map the ledger or approve the proposal first."
        )
    return problems
