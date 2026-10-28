"""The bridge agent against a stand-in for TallyPrime's XML interface.

Tally is a desktop program, so these tests fake its two jobs: answer an export
query, and accept or reject an import. That is enough to prove the parts that
matter for autonomy — a voucher posts, a retry cannot post twice, a rejected
voucher carries Tally's own words back, and nothing is lost when Tally is closed.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bridge_agent.agent import expired
from bridge_agent.queue import LocalQueue
from bridge_agent.tally import (
    TallyAdapter,
    TallyUnavailable,
    build_reversal_xml,
)

VOUCHER_XML = """<ENVELOPE>
 <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
 <BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME></REQUESTDESC>
 <REQUESTDATA><TALLYMESSAGE>
  <VOUCHER VCHTYPE="Purchase" ACTION="Create">
   <DATE>20261017</DATE>
   <NARRATION>ADE/INV/1 SHREE STEEL TUBES</NARRATION>
   <ALLLEDGERENTRIES.LIST><LEDGERNAME>Purchase</LEDGERNAME><AMOUNT>105340.00</AMOUNT></ALLLEDGERENTRIES.LIST>
   <ALLLEDGERENTRIES.LIST><LEDGERNAME>Supplier</LEDGERNAME><AMOUNT>-105340.00</AMOUNT></ALLLEDGERENTRIES.LIST>
  </VOUCHER>
 </TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY>
</ENVELOPE>"""


class FakeTally(BaseHTTPRequestHandler):
    """One endpoint that behaves like TallyPrime's XML interface."""

    mode = "ok"
    register: list[str] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        kind = self.server.kind  # type: ignore[attr-defined]

        if "Export Data" in body:
            # A voucher register: include a posted voucher only when a duplicate
            # has been simulated, so idempotency can be checked.
            if kind == "duplicate":
                payload = (
                    "<ENVELOPE><BODY><DATA><COLLECTION>"
                    "<VOUCHER><VOUCHERNUMBER>PUR/2026/0007</VOUCHERNUMBER>"
                    "<NARRATION>ADE/INV/1 SHREE STEEL TUBES</NARRATION></VOUCHER>"
                    "</COLLECTION></DATA></BODY></ENVELOPE>"
                )
            else:
                payload = "<ENVELOPE><BODY><DATA></DATA></BODY></ENVELOPE>"
            self._respond(payload)
            return

        if kind == "rejected":
            payload = (
                "<ENVELOPE><BODY><DATA><LINEERROR>Ledger 'Purchase' does not exist</LINEERROR>"
                "</DATA></BODY></ENVELOPE>"
            )
            self._respond(payload)
            return

        if kind == "offline":
            self.server.shutdown_request(self.request)  # type: ignore[attr-defined]
            return

        payload = (
            "<ENVELOPE><BODY><DATA><IMPORTRESULT>"
            "<CREATED>1</CREATED><ALTERED>0</ALTERED>"
            "<LASTVCHID>88231</LASTVCHID><VOUCHERNUMBER>PUR/2026/0008</VOUCHERNUMBER>"
            "</IMPORTRESULT></DATA></BODY></ENVELOPE>"
        )
        self._respond(payload)

    def _respond(self, payload: str) -> None:
        raw = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args) -> None:  # silence the test output
        return


@pytest.fixture()
def tally(kind: str = "ok"):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeTally)
    server.kind = kind  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", kind
    finally:
        server.shutdown()
        server.server_close()


def _adapter(url: str) -> TallyAdapter:
    return TallyAdapter(base_url=url, company="Precision Pipes Pvt Ltd", timeout=5)


def test_a_voucher_posts_and_tally_returns_its_number(tally):
    url, _ = tally

    async def run():
        async with _adapter(url) as adapter:
            return await adapter.post_voucher(VOUCHER_XML)

    reply = asyncio.run(run())
    assert reply.ok
    assert reply.voucher_number == "PUR/2026/0008"
    assert reply.voucher_id == "88231"
    assert reply.errors == []


def test_a_rejected_voucher_returns_tally_words_in_plain_language():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeTally)
    server.kind = "rejected"  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"

    async def run():
        async with _adapter(url) as adapter:
            return await adapter.post_voucher(VOUCHER_XML)

    try:
        reply = asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()

    assert not reply.ok
    assert "Ledger 'Purchase' does not exist" in (reply.plain_error or "")


def test_an_existing_voucher_with_the_same_reference_is_found_so_retries_do_not_duplicate():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeTally)
    server.kind = "duplicate"  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"

    async def run():
        async with _adapter(url) as adapter:
            return await adapter.existing_voucher_with_reference("ADE/INV/1 SHREE STEEL TUBES")

    try:
        found = asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()

    assert found == "PUR/2026/0007"


def test_a_closed_tally_is_a_deferral_not_a_failure():
    async def run():
        async with TallyAdapter(base_url="http://127.0.0.1:1", timeout=1) as adapter:
            return await adapter.post_voucher(VOUCHER_XML)

    with pytest.raises(TallyUnavailable) as raised:
        asyncio.run(run())
    assert "TallyPrime could not be reached" in str(raised.value)
    # The agent defers on this, so the voucher waits instead of being lost.
    assert "XML interface" in str(raised.value)


def test_reversal_cancels_by_reference_so_the_audit_trail_survives():
    xml = build_reversal_xml("PUR/2026/0008", "ADE/INV/1", "Precision Pipes Pvt Ltd")
    assert 'ACTION="Cancel"' in xml
    assert "<VOUCHERNUMBER>PUR/2026/0008</VOUCHERNUMBER>" in xml
    assert "ADE/INV/1" in xml
    assert "<SVCURRENTCOMPANY>Precision Pipes Pvt Ltd</SVCURRENTCOMPANY>" in xml


def test_the_local_queue_survives_a_restart_and_ignores_a_repeat_command(tmp_path):
    path = tmp_path / "queue.sqlite3"
    command = {
        "id": "cmd-1",
        "kind": "post_voucher",
        "idempotency_key": "tenant:record:Purchase",
        "payload": {"xml": VOUCHER_XML},
    }

    queue = LocalQueue(path)
    assert queue.enqueue(command) is True
    # The cloud may resend a command after a bad connection; the queue keeps one.
    assert queue.enqueue(command) is False
    queue.close()

    reopened = LocalQueue(path)
    try:
        pending = reopened.pending()
        assert [c.command_id for c in pending] == ["cmd-1"]
        reopened.defer("cmd-1", error="Tally was closed")
        # A deferral keeps the command pending for the next cycle, with its reason.
        still_pending = reopened.pending()
        assert still_pending[0].last_error == "Tally was closed"
        assert still_pending[0].attempts == 1

        reopened.store_result("cmd-1", "acked", {"result": {"voucher_number": "PUR/2026/0008"}})
        results = reopened.undelivered_results()
        body = results[0]["body"]
        body = json.loads(body) if isinstance(body, str) else body
        assert body["result"]["voucher_number"] == "PUR/2026/0008"
    finally:
        reopened.close()


def test_an_expired_command_is_dropped_rather_than_posted_late(tmp_path):
    queue = LocalQueue(tmp_path / "queue.sqlite3")
    try:
        queue.enqueue(
            {
                "id": "cmd-old",
                "kind": "post_voucher",
                "idempotency_key": "tenant:record:Purchase",
                "expires_at": "2020-01-01T00:00:00+00:00",
                "payload": {"xml": VOUCHER_XML},
            }
        )
        command = queue.pending()[0]
        assert expired(command) is True
        queue.expire(command.command_id, reason="the command expired before it was posted")
        assert queue.pending() == []
    finally:
        queue.close()
