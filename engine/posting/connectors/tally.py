"""TallyPrime connectors (section 6.5a, Appendix C).

Two modes, one voucher shape:

* ``TallyBridgeConnector`` (Mode A, primary) queues a signed command for the local
  agent, which posts to TallyPrime's XML interface on the customer's PC.
* ``TallyFileConnector`` (Mode B, fallback) renders a Tally-compatible XML import
  file for the user to import by hand.

Both build the voucher from the same ``VoucherDraft``, so a reviewer sees exactly
what will be written either way.

Tally's import format notes that matter:

* Voucher dates are ``YYYYMMDD``.
* A ledger entry's ``ISDEEMEDPOSITIVE`` is ``Yes`` for a debit; Tally's signed
  amount convention is a negative amount for a debit and a positive amount for a
  credit.
* Inventory lines need both ``ACTUALQTY`` and ``BILLEDQTY`` with a unit, or Tally
  rejects the voucher, so both are always written for stock lines.
"""

from __future__ import annotations

import csv
import io
import uuid
import xml.sax.saxutils as saxutils
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg

from engine.models import VoucherDraft, VoucherLine
from engine.posting.commands import create_command
from engine.posting.connectors.base import PostContext, PostResult, basic_validation

TWO = Decimal("0.01")


def _amount(value: Decimal) -> str:
    return f"{value.quantize(TWO):.2f}"


def _quantity(value: Decimal | None) -> str:
    if value is None:
        return ""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _esc(value: Any) -> str:
    return saxutils.escape(str(value if value is not None else ""))


def voucher_xml(draft: VoucherDraft, *, company: str | None) -> str:
    """The ``<VOUCHER>`` element for one draft, ready for import."""
    lines: list[str] = []
    lines.append(f'     <VOUCHER VCHTYPE="{_esc(draft.voucher_type)}" ACTION="Create">')
    if draft.voucher_date:
        lines.append(f"      <DATE>{draft.voucher_date.strftime('%Y%m%d')}</DATE>")
    lines.append(f"      <VOUCHERTYPENAME>{_esc(draft.voucher_type)}</VOUCHERTYPENAME>")
    if draft.narration:
        lines.append(f"      <NARRATION>{_esc(draft.narration)}</NARRATION>")
    lines.append(f"      <REFERENCE>{_esc(draft.idempotency_key)}</REFERENCE>")
    if draft.party_ledger:
        lines.append(f"      <PARTYLEDGERNAME>{_esc(draft.party_ledger)}</PARTYLEDGERNAME>")
    if draft.place_of_supply:
        lines.append(
            f"      <PLACEOFSUPPLY>{_esc(draft.place_of_supply)}</PLACEOFSUPPLY>"
        )

    for line in draft.lines:
        lines.append("      <ALLLEDGERENTRIES.LIST>")
        lines.append(f"        <LEDGERNAME>{_esc(line.ledger or line.stock_item)}</LEDGERNAME>")
        lines.append(
            f"        <ISDEEMEDPOSITIVE>{'Yes' if line.is_debit else 'No'}</ISDEEMEDPOSITIVE>"
        )
        signed = -line.amount if line.is_debit else line.amount
        lines.append(f"        <AMOUNT>{_amount(signed)}</AMOUNT>")
        if line.description:
            lines.append(f"        <NARRATION>{_esc(line.description)}</NARRATION>")
        lines.append("      </ALLLEDGERENTRIES.LIST>")

    for line in draft.lines:
        if not line.stock_item:
            continue
        quantity = _quantity(line.quantity)
        unit = line.unit or "NOS"
        lines.append("      <ALLINVENTORYENTRIES.LIST>")
        lines.append(f"        <STOCKITEMNAME>{_esc(line.stock_item)}</STOCKITEMNAME>")
        lines.append(
            f"        <ISDEEMEDPOSITIVE>{'Yes' if line.is_debit else 'No'}</ISDEEMEDPOSITIVE>"
        )
        signed = -line.amount if line.is_debit else line.amount
        lines.append(f"        <RATE>{_amount(line.rate or Decimal('0'))}/NOS</RATE>")
        lines.append(f"        <AMOUNT>{_amount(signed)}</AMOUNT>")
        lines.append(f"        <ACTUALQTY>{quantity} {_esc(unit)}</ACTUALQTY>")
        lines.append(f"        <BILLEDQTY>{quantity} {_esc(unit)}</BILLEDQTY>")
        lines.append("      </ALLINVENTORYENTRIES.LIST>")

    lines.append("     </VOUCHER>")
    return "\n".join(lines)


def envelope(vouchers: list[VoucherDraft], *, company: str | None = None) -> str:
    """A complete import envelope around one or more vouchers."""
    body = "\n".join(voucher_xml(draft, company=company) for draft in vouchers)
    company_block = (
        f"<STATICVARIABLES><SVCURRENTCOMPANY>{_esc(company)}</SVCURRENTCOMPANY></STATICVARIABLES>"
        if company
        else ""
    )
    return f"""<ENVELOPE>
 <HEADER>
  <TALLYREQUEST>Import Data</TALLYREQUEST>
 </HEADER>
 <BODY>
  <IMPORTDATA>
   <REQUESTDESC>
    <REPORTNAME>Vouchers</REPORTNAME>
    {company_block}
   </REQUESTDESC>
   <REQUESTDATA>
    <TALLYMESSAGE xmlns:UDF="TallyUDF">
{body}
    </TALLYMESSAGE>
   </REQUESTDATA>
  </IMPORTDATA>
 </BODY>
</ENVELOPE>
"""


def draft_to_csv_rows(draft: VoucherDraft) -> list[dict[str, str]]:
    """A flat representation, for the CSV/Excel export and for humans."""
    rows: list[dict[str, str]] = []
    for index, line in enumerate(draft.lines, start=1):
        rows.append(
            {
                "voucher_type": draft.voucher_type,
                "voucher_date": draft.voucher_date.isoformat() if draft.voucher_date else "",
                "reference": draft.idempotency_key,
                "party": draft.party_ledger or "",
                "line_no": str(index),
                "ledger": line.ledger or "",
                "stock_item": line.stock_item or "",
                "debit": _amount(line.amount) if line.is_debit else "",
                "credit": "" if line.is_debit else _amount(line.amount),
                "quantity": _quantity(line.quantity),
                "unit": line.unit or "",
                "rate": _amount(line.rate) if line.rate is not None else "",
                "gst_rate": str(line.gst_rate) if line.gst_rate is not None else "",
                "narration": draft.narration or "",
            }
        )
    return rows


def rows_to_csv(rows: list[dict[str, str]]) -> str:
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


class TallyBridgeConnector:
    """Mode A: autonomous posting through the customer's local agent."""

    name = "tally_bridge"
    label = "TallyPrime (local bridge agent)"

    def __init__(self, cur: psycopg.Cursor | None = None) -> None:
        self.cur = cur

    async def validate(self, ctx: PostContext) -> list[str]:
        problems = basic_validation(ctx)
        if ctx.agent is None:
            problems.append(
                "No bridge agent is connected for this tenant, so nothing can be posted "
                "to Tally right now. Use the file import, or bring the agent online."
            )
        elif ctx.agent.get("status") == "revoked":
            problems.append("This tenant's bridge agent has been revoked.")
        elif ctx.agent.get("mode") == "read_only":
            problems.append("The bridge agent is in read-only mode, so it will not post.")
        if not (ctx.draft.tally_company or (ctx.agent or {}).get("tally_company")):
            problems.append(
                "The Tally company is not known. The agent must report which company it "
                "has open before posting."
            )
        return problems

    async def post(self, ctx: PostContext) -> PostResult:
        if self.cur is None:
            return PostResult(
                ok=False,
                status="transport_error",
                connector=self.name,
                error_text="the command queue is unavailable",
                require_human=True,
            )
        problems = await self.validate(ctx)
        if problems:
            return PostResult(
                ok=False,
                status="tally_rejected",
                connector=self.name,
                error_text=" ".join(problems),
                error_code="precondition_failed",
                require_human=True,
            )

        draft = ctx.draft.model_dump(mode="json")
        command = await create_command(
            self.cur,
            tenant_id=ctx.tenant.id,
            agent_id=ctx.agent["id"] if ctx.agent else None,
            kind="post_voucher",
            idempotency_key=ctx.draft.idempotency_key,
            payload={
                "voucher": draft,
                "company": ctx.draft.tally_company or (ctx.agent or {}).get("tally_company"),
                "mode": ctx.mode,
                "xml": voucher_xml(ctx.draft, company=ctx.draft.tally_company),
            },
        )
        return PostResult(
            ok=True,
            status="queued",
            connector=self.name,
            response={
                "command_id": str(command["id"]),
                "agent_id": str(command["agent_id"]) if command["agent_id"] else None,
                "expires_at": command["expires_at"].isoformat() if command["expires_at"] else None,
                "signed": bool(command["signature"]),
            },
            error_text=None
            if command["signature"]
            else "The command could not be signed; the signing keypair is not configured.",
        )

    async def confirm(self, ctx: PostContext, result: PostResult) -> PostResult:
        return result

    async def roll_back(self, ctx: PostContext, result: PostResult) -> PostResult:
        if self.cur is None or ctx.voucher_id is None:
            return PostResult(
                ok=False,
                status="transport_error",
                connector=self.name,
                error_text="cannot queue a reversal without the command queue",
                require_human=True,
            )
        command = await create_command(
            self.cur,
            tenant_id=ctx.tenant.id,
            agent_id=ctx.agent["id"] if ctx.agent else None,
            kind="reverse_voucher",
            idempotency_key=f"{ctx.draft.idempotency_key}:reverse",
            payload={
                "voucher_number": result.tally_voucher_number,
                "voucher_id": result.tally_voucher_id,
                "company": ctx.draft.tally_company or (ctx.agent or {}).get("tally_company"),
                "reference": ctx.draft.idempotency_key,
                "voice": "Reverse the voucher whose narration carries this reference.",
            },
        )
        return PostResult(
            ok=True,
            status="queued",
            connector=self.name,
            response={"command_id": str(command["id"]), "action": "reverse"},
        )


class TallyFileConnector:
    """Mode B: produce an import file for the user to bring into Tally."""

    name = "tally_file"
    label = "TallyPrime (file import)"

    async def validate(self, ctx: PostContext) -> list[str]:
        return basic_validation(ctx)

    async def post(self, ctx: PostContext) -> PostResult:
        problems = await self.validate(ctx)
        if problems:
            return PostResult(
                ok=False,
                status="tally_rejected",
                connector=self.name,
                error_text=" ".join(problems),
                error_code="precondition_failed",
                require_human=True,
            )
        xml = envelope([ctx.draft], company=ctx.draft.tally_company)
        return PostResult(
            ok=True,
            status="staged",
            connector=self.name,
            response={
                "format": "tally_xml",
                "file_name": f"{ctx.draft.idempotency_key.replace(':', '_')}.xml",
                "xml": xml,
                "csv": rows_to_csv(draft_to_csv_rows(ctx.draft)),
            },
            require_human=True,
        )

    async def confirm(self, ctx: PostContext, result: PostResult) -> PostResult:
        # Nothing to read back: the human imports the file and Tally owns the
        # voucher from there. Marking it confirmed is the reviewer's action.
        return result

    async def roll_back(self, ctx: PostContext, result: PostResult) -> PostResult:
        return PostResult(
            ok=False,
            status="cancelled",
            connector=self.name,
            error_text=(
                "A file-imported voucher is reversed in Tally itself; the engine has no "
                "handle on it. Record the reversal so the audit trail stays complete."
            ),
            require_human=True,
        )


class ShadowConnector:
    """Autonomy stage 1: record what would have been posted and stop."""

    name = "shadow"
    label = "Shadow mode"

    async def validate(self, ctx: PostContext) -> list[str]:
        return basic_validation(ctx)

    async def post(self, ctx: PostContext) -> PostResult:
        return PostResult(
            ok=True,
            status="shadow",
            connector=self.name,
            response={
                "would_post": ctx.draft.model_dump(mode="json"),
                "xml": envelope([ctx.draft], company=ctx.draft.tally_company),
                "note": "Shadow mode: nothing was sent to Tally.",
            },
        )

    async def confirm(self, ctx: PostContext, result: PostResult) -> PostResult:
        return result

    async def roll_back(self, ctx: PostContext, result: PostResult) -> PostResult:
        return PostResult(
            ok=True, status="cancelled", connector=self.name, response={"note": "nothing to reverse"}
        )


def tally_voucher_date(value: date | None) -> str:
    return value.strftime("%Y%m%d") if value else ""


def new_reference() -> str:
    return uuid.uuid4().hex[:12]


def summarise_lines(lines: list[VoucherLine]) -> dict[str, Any]:
    return {
        "line_count": len(lines),
        "debit_total": str(sum((l.amount for l in lines if l.is_debit), Decimal("0"))),
        "credit_total": str(sum((l.amount for l in lines if not l.is_debit), Decimal("0"))),
    }
