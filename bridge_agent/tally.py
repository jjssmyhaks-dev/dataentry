"""The Tally adapter (Appendix E).

Talks to TallyPrime through its local XML interface: builds voucher XML, posts it,
reads masters, and parses Tally's replies into plain language.

Tally's behaviour differs across builds, so every reply is parsed defensively and
an unparseable reply is reported as an error rather than treated as success. The
XML shape itself comes from the pack's connector definitions, and the same builder
is used for the file-import fallback, so both modes send equivalent vouchers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

SUCCESS_MARKERS = ("<CREATED>1</CREATED>", "<ALTERED>1</ALTERED>", "SUCCESS", "<EXCEPTIONS>0</EXCEPTIONS>")
ERROR_TAGS = ("LINEERROR", "ERROR", "EXCEPTION", "LINEERRORS")


@dataclass
class TallyReply:
    ok: bool
    status_code: int
    raw: str = ""
    voucher_number: str | None = None
    voucher_id: str | None = None
    company: str | None = None
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    @property
    def plain_error(self) -> str | None:
        """Tally's error text, in words a reviewer can act on."""
        if self.ok:
            return None
        if self.errors:
            return "; ".join(self.errors[:3])
        if self.status_code >= 400:
            return f"TallyPrime answered with HTTP {self.status_code}."
        if not self.raw.strip():
            return "TallyPrime sent an empty reply; the XML interface may be disabled."
        return "TallyPrime did not confirm the voucher. Check that the company is open and the period is not locked."


class TallyUnavailable(RuntimeError):
    """Tally is closed, or its XML interface is switched off."""


class TallyAdapter:
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:9000",
        company: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.company = company
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "TallyAdapter":
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _send(self, xml: str) -> TallyReply:
        if self._client is None:
            raise RuntimeError("use TallyAdapter as an async context manager")
        try:
            response = await self._client.post(
                self.base_url,
                content=xml.encode("utf-8"),
                headers={"Content-Type": "text/xml; charset=utf-8"},
            )
        except httpx.HTTPError as exc:
            raise TallyUnavailable(
                f"TallyPrime could not be reached at {self.base_url}: {type(exc).__name__}. "
                "Is TallyPrime open, and is its XML interface enabled?"
            ) from exc
        return self._parse(response.text, response.status_code)

    def _parse(self, raw: str, status_code: int) -> TallyReply:
        errors: list[str] = []
        for tag in ERROR_TAGS:
            for match in re.finditer(rf"<{tag}>(.*?)</{tag}>", raw, re.S | re.I):
                text = re.sub(r"<[^>]+>", " ", match.group(1))
                text = " ".join(text.split())
                if text and text not in {"0", "1"}:
                    errors.append(text)
        voucher_number = _first(raw, ("VOUCHERNUMBER", "VOUCHERNO", "MASTERVOUCHERNUMBER"))
        # TallyPrime answers an import with LASTVCHID; exports use MASTERID.
        voucher_id = _first(raw, ("LASTVCHID", "MASTERID", "VOUCHERKEY", "VOUCHERID", "VCHID"))
        company = _first(raw, ("SVCURRENTCOMPANY", "CMPNAME", "COMPANYNAME"))
        created = _first(raw, ("CREATED", "ALTERED", "EXCEPTIONS"))
        ok = not errors and (
            any(marker in raw.upper() for marker in SUCCESS_MARKERS)
            or (created == "1")
            or status_code == 200 and bool(voucher_number)
        )
        return TallyReply(
            ok=ok,
            status_code=status_code,
            raw=raw[:4000],
            voucher_number=voucher_number,
            voucher_id=voucher_id,
            company=company,
            errors=errors,
        )

    # ── operations ──────────────────────────────────────────────────────────

    async def post_voucher(self, xml: str) -> TallyReply:
        return await self._send(xml)

    async def reverse_voucher(self, xml: str) -> TallyReply:
        return await self._send(xml)

    async def company_state(self) -> TallyReply:
        """Which company is open, so the agent can confirm before posting."""
        return await self._send(
            """<ENVELOPE>
 <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
 <BODY><EXPORTDATA><REQUESTDESC>
   <REPORTNAME>List of Companies</REPORTNAME>
 </REQUESTDESC><REQUESTDATA></REQUESTDATA></EXPORTDATA></BODY>
</ENVELOPE>"""
        )

    async def open_company(self) -> str | None:
        reply = await self.company_state()
        companies = re.findall(r"<COMPANYNAME>(.*?)</COMPANYNAME>", reply.raw, re.I)
        if not companies:
            companies = re.findall(r"<SVCURRENTCOMPANY>(.*?)</SVCURRENTCOMPANY>", reply.raw, re.I)
        active = _first(reply.raw, ("SVCURRENTCOMPANY",))
        if active:
            return active
        return companies[0] if companies else None

    async def read_masters(self, report: str) -> list[dict[str, Any]]:
        """Read ledger, party or stock masters back so extraction and validation
        match the customer's own data."""
        xml = f"""<ENVELOPE>
 <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
 <BODY><EXPORTDATA><REQUESTDESC>
   <REPORTNAME>{report}</REPORTNAME>
   <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
     {f'<SVCURRENTCOMPANY>{self.company}</SVCURRENTCOMPANY>' if self.company else ''}
   </STATICVARIABLES>
 </REQUESTDESC><REQUESTDATA></REQUESTDATA></EXPORTDATA></BODY>
</ENVELOPE>"""
        reply = await self._send(xml)
        return _elements(reply.raw)

    async def existing_voucher_with_reference(self, reference: str) -> str | None:
        """Idempotency: before posting, look for a voucher whose narration already
        carries this reference, so a retry cannot create a duplicate."""
        if not reference:
            return None
        xml = f"""<ENVELOPE>
 <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
 <BODY><EXPORTDATA><REQUESTDESC>
   <REPORTNAME>Voucher Register</REPORTNAME>
   <STATICVARIABLES>
     <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
     <SVFROMDATE>20000401</SVFROMDATE>
     <SVTODATE>20991231</SVTODATE>
     {f'<SVCURRENTCOMPANY>{self.company}</SVCURRENTCOMPANY>' if self.company else ''}
   </STATICVARIABLES>
 </REQUESTDESC><REQUESTDATA></REQUESTDATA></EXPORTDATA></BODY>
</ENVELOPE>"""
        reply = await self._send(xml)
        if reference in reply.raw:
            match = re.search(
                rf"<VOUCHERNUMBER>(.*?)</VOUCHERNUMBER>(?:(?!<VOUCHER).)*{re.escape(reference)}",
                reply.raw,
                re.S | re.I,
            )
            return match.group(1) if match else "existing"
        return None


def _first(raw: str, tags: tuple[str, ...]) -> str | None:
    for tag in tags:
        match = re.search(rf"<{tag}>(.*?)</{tag}>", raw, re.S | re.I)
        if match:
            value = " ".join(match.group(1).split())
            if value:
                return value
    return None


def _elements(raw: str) -> list[dict[str, Any]]:
    """Flatten simple Tally list replies into dictionaries."""
    items: list[dict[str, Any]] = []
    for block in re.findall(r"<(LEDGER|STOCKITEM|GROUP|COMPANY|COSTCENTRE|GODOWN)\b[^>]*>(.*?)</\1>", raw, re.S | re.I):
        kind, body = block
        record: dict[str, Any] = {"kind": kind.upper()}
        for tag in ("NAME", "PARENT", "GSTREGISTRATIONNUMBER", "GSTIN", "PARTYGSTIN", "OPENINGBALANCE", "BASEUNITS", "RATEOFTAXCALCULATION"):
            value = _first(body, (tag,))
            if value:
                record[tag.lower()] = value
        if record.get("name"):
            items.append(record)
    return items


def build_reversal_xml(voucher_number: str | None, reference: str, company: str | None) -> str:
    """A cancellation rather than a delete, so the audit trail in Tally survives."""
    company_block = f"<SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>" if company else ""
    selector = (
        f"<VOUCHERNUMBER>{voucher_number}</VOUCHERNUMBER>" if voucher_number else "<NARRATION>" + reference + "</NARRATION>"
    )
    return f"""<ENVELOPE>
 <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
 <BODY><IMPORTDATA><REQUESTDESC>
   <REPORTNAME>Vouchers</REPORTNAME>
   <STATICVARIABLES>{company_block}</STATICVARIABLES>
 </REQUESTDESC><REQUESTDATA>
  <TALLYMESSAGE xmlns:UDF="TallyUDF">
   <VOUCHER VCHTYPE="Purchase" ACTION="Cancel">
    {selector}
    <NARRATION>Reversed by the engine, reference {reference}</NARRATION>
   </VOUCHER>
  </TALLYMESSAGE>
 </REQUESTDATA></IMPORTDATA></BODY>
</ENVELOPE>"""
