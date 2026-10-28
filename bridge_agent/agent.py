"""The bridge agent loop and supervisor (Appendix E).

The post flow it implements, step by step:

1. the cloud decides a record is ready and creates a signed command
2. the agent receives it on its outbound connection
3. it verifies the signature, confirms the right company is open and the period is
   not locked, then checks whether this voucher was already posted
4. the Tally adapter posts it and reads the voucher identifier back
5. the result goes to the cloud, which marks the record posted or routes it to the
   review queue with Tally's error in plain words
6. if the connection is down, the command and the result wait locally
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import platform
import socket
import time
from typing import Any

from bridge_agent.client import CloudClient, CloudUnavailable
from bridge_agent.config import AgentConfig
from bridge_agent.queue import LocalQueue, QueuedCommand
from bridge_agent.tally import TallyAdapter, TallyUnavailable, build_reversal_xml

log = logging.getLogger("bridge-agent")


def verify_command(payload: dict[str, Any], public_key: str) -> tuple[bool, str | None]:
    """Check the cloud's signature over the command's own fields."""
    signature = payload.get("signature")
    if not signature:
        return False, "the command carries no signature"
    if not public_key:
        return False, "this agent has no cloud public key, so it cannot verify commands"
    body = {k: v for k, v in payload.items() if k != "signature"}
    try:
        from engine.posting.commands import verify_signature

        ok = verify_signature(body, signature, key=public_key)
    except Exception as exc:  # pragma: no cover - depends on cryptography
        return False, f"the signature could not be checked: {type(exc).__name__}"
    return (True, None) if ok else (False, "the command signature does not match")


def expired(command: QueuedCommand, *, now: float | None = None) -> bool:
    if not command.expires_at:
        return False
    try:
        deadline = dt.datetime.fromisoformat(command.expires_at)
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=dt.timezone.utc)
    return deadline < dt.datetime.now(dt.timezone.utc)


class BridgeAgent:
    def __init__(self, config: AgentConfig, *, queue: LocalQueue | None = None) -> None:
        self.config = config
        self.queue = queue or LocalQueue(config.queue_path)
        self.cloud = CloudClient(config.cloud_url, config.credential_id)
        self.last_post_at: float | None = None
        self.last_error: str | None = None
        self.connected = False
        self._stop = asyncio.Event()

    # ── status ──────────────────────────────────────────────────────────────

    def write_status(self, **extra: Any) -> None:
        payload = {
            "agent": self.config.credential_id,
            "app_version": self.config.app_version,
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "connected": self.connected,
            "tally_url": self.config.tally_url,
            "company": self.config.tally_company,
            "last_post_at": (
                dt.datetime.fromtimestamp(self.last_post_at, dt.timezone.utc).isoformat()
                if self.last_post_at
                else None
            ),
            "last_error": self.last_error,
            "queue": self.queue.counts(),
            **extra,
        }
        self.config.status_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    # ── one cycle ───────────────────────────────────────────────────────────

    async def cycle(self) -> dict[str, Any]:
        """One heartbeat, fetch, deliver round trip. Never raises on connectivity."""
        state = await self._tally_state()
        summary: dict[str, Any] = {"posted": 0, "failed": 0, "deferred": 0, "expired": 0}

        try:
            await self.cloud.hello()
            await self.cloud.heartbeat(state)
            self.connected = True
            self.last_error = None
        except CloudUnavailable as exc:
            self.connected = False
            self.last_error = str(exc)
            self.queue.log("warn", "cloud unreachable", {"error": str(exc)})

        # Deliver anything the cloud has not seen yet, even before fetching more.
        for result in self.queue.undelivered_results():
            body = result["body"]
            try:
                await self.cloud.report_result(
                    result["command_id"],
                    status=result["status"],
                    result=body.get("result", {}),
                    error_text=body.get("error_text"),
                    customer_payload=body.get("payload"),
                    signature=body.get("signature"),
                )
                self.queue.mark_result_delivered(result["command_id"])
            except CloudUnavailable:
                break

        if self.connected:
            try:
                for command in await self.cloud.commands(limit=20):
                    if self.queue.enqueue(command):
                        self.queue.log("info", "command received", {"kind": command.get("command_kind")})
            except CloudUnavailable as exc:
                self.connected = False
                self.last_error = str(exc)

        for command in self.queue.pending():
            if expired(command):
                self.queue.expire(command.command_id, reason="the command expired before it was posted")
                self.queue.store_result(
                    command.command_id,
                    "expired",
                    {"result": {}, "error_text": "command expired; the record returns to the review queue"},
                )
                summary["expired"] += 1
                continue

            outcome = await self._handle(command, state)
            summary[outcome] = summary.get(outcome, 0) + 1

        self.write_status(**summary)
        return summary

    # ── command handling ────────────────────────────────────────────────────

    async def _handle(self, command: QueuedCommand, state: dict[str, Any]) -> str:
        payload = command.payload
        ok, reason = verify_command(payload, self.config.cloud_public_key)
        if not ok:
            self.queue.mark(command.command_id, "rejected", error=reason)
            self.queue.log("error", "command refused", {"reason": reason})
            return "failed"

        if command.kind == "pause":
            self.queue.mark(command.command_id, "done")
            self.queue.log("info", "paused by the cloud")
            self._stop.set()
            return "deferred"

        if command.kind == "health_check":
            self.queue.mark(command.command_id, "done")
            self.queue.store_result(command.command_id, "acked", {"result": state})
            return "posted"

        if command.kind == "sync_masters":
            return await self._sync_masters(command)

        if not state["tally_running"]:
            self.queue.defer(command.command_id, error="TallyPrime is not running; the command waits")
            return "deferred"

        if self.config.require_company_match and not state["period_open"]:
            self.queue.defer(
                command.command_id,
                error="the financial period is not open, or the company name does not match",
            )
            return "deferred"

        if command.kind == "post_voucher":
            return await self._post(command)
        if command.kind == "reverse_voucher":
            return await self._reverse(command)

        self.queue.mark(command.command_id, "rejected", error=f"unknown command kind {command.kind}")
        return "failed"

    async def _post(self, command: QueuedCommand) -> str:
        payload = command.payload
        xml = payload.get("xml")
        if not xml:
            self.queue.mark(command.command_id, "rejected", error="the command carried no voucher XML")
            return "failed"

        reference = command.idempotency_key or payload.get("idempotency_key") or ""
        try:
            async with TallyAdapter(
                base_url=self.config.tally_url,
                company=payload.get("company") or self.config.tally_company,
                timeout=self.config.tally_timeout_seconds,
            ) as tally:
                # Idempotency: never create a second voucher for the same reference.
                existing = await tally.existing_voucher_with_reference(reference)
                if existing:
                    self.queue.mark(command.command_id, "done")
                    self.queue.store_result(
                        command.command_id,
                        "acked",
                        {
                            "result": {
                                "voucher_number": existing,
                                "note": "already posted; retry was ignored",
                            },
                            "payload": {},
                        },
                    )
                    self.queue.log("info", "duplicate post avoided", {"reference": reference})
                    return "posted"

                reply = await tally.post_voucher(xml)
        except TallyUnavailable as exc:
            self.queue.defer(command.command_id, error=str(exc))
            return "deferred"
        except Exception as exc:  # pragma: no cover - unexpected adapter failure
            self.queue.mark(command.command_id, "failed", error=f"{type(exc).__name__}: {exc}")
            return "failed"

        if reply.ok:
            self.queue.mark(command.command_id, "done")
            self.queue.store_result(
                command.command_id,
                "acked",
                {
                    "result": {
                        "voucher_number": reply.voucher_number,
                        "voucher_id": reply.voucher_id,
                        "company": reply.company or self.config.tally_company,
                    },
                    "payload": {},
                },
            )
            self.last_post_at = time.time()
            self.queue.log("info", "voucher posted", {"voucher_number": reply.voucher_number})
            return "posted"

        # A rejected voucher carries Tally's own words back to the queue.
        self.queue.mark(command.command_id, "failed", error=reply.plain_error)
        self.queue.store_result(
            command.command_id,
            "failed",
            {
                "result": {"tally_raw": reply.raw[:2000], "status_code": reply.status_code},
                "error_text": reply.plain_error,
                "payload": {},
            },
        )
        self.queue.log("error", "tally rejected the voucher", {"error": reply.plain_error})
        return "failed"

    async def _reverse(self, command: QueuedCommand) -> str:
        payload = command.payload
        xml = build_reversal_xml(
            payload.get("voucher_number"),
            payload.get("reference") or command.idempotency_key or "",
            payload.get("company") or self.config.tally_company,
        )
        try:
            async with TallyAdapter(
                base_url=self.config.tally_url,
                company=payload.get("company") or self.config.tally_company,
                timeout=self.config.tally_timeout_seconds,
            ) as tally:
                reply = await tally.reverse_voucher(xml)
        except TallyUnavailable as exc:
            self.queue.defer(command.command_id, error=str(exc))
            return "deferred"

        if reply.ok:
            self.queue.mark(command.command_id, "done")
            self.queue.store_result(
                command.command_id,
                "acked",
                {"result": {"reversed": True, "voucher_number": payload.get("voucher_number")}, "payload": {}},
            )
            self.queue.log("info", "voucher reversed", {"voucher_number": payload.get("voucher_number")})
            return "posted"

        self.queue.mark(command.command_id, "failed", error=reply.plain_error)
        self.queue.store_result(
            command.command_id,
            "failed",
            {"result": {"tally_raw": reply.raw[:2000]}, "error_text": reply.plain_error, "payload": {}},
        )
        return "failed"

    async def _sync_masters(self, command: QueuedCommand) -> str:
        reports = {
            "ledgers": "List of Ledgers",
            "parties": "List of Ledgers",
            "stock_items": "List of Stock Items",
            "companies": "List of Companies",
        }
        collected: dict[str, Any] = {"company": self.config.tally_company}
        try:
            async with TallyAdapter(
                base_url=self.config.tally_url,
                company=self.config.tally_company,
                timeout=self.config.tally_timeout_seconds,
            ) as tally:
                for kind, report in reports.items():
                    collected[kind] = await tally.read_masters(report)
        except TallyUnavailable as exc:
            self.queue.defer(command.command_id, error=str(exc))
            return "deferred"

        try:
            await self.cloud.sync_masters(collected)
        except CloudUnavailable as exc:
            self.queue.defer(command.command_id, error=f"masters read but not delivered: {exc}")
            return "deferred"

        self.queue.mark(command.command_id, "done")
        self.queue.store_result(
            command.command_id,
            "acked",
            {"result": {"synced": {k: len(v) for k, v in collected.items() if isinstance(v, list)}}, "payload": {}},
        )
        return "posted"

    # ── Tally state ─────────────────────────────────────────────────────────

    async def _tally_state(self) -> dict[str, Any]:
        state = {
            "tally_running": False,
            "company_open": None,
            "period_open": True,
            "queue_depth": len(self.queue.pending()),
            "last_post_at": (
                dt.datetime.fromtimestamp(self.last_post_at, dt.timezone.utc).isoformat()
                if self.last_post_at
                else None
            ),
            "app_version": self.config.app_version,
            "host_name": socket.gethostname(),
            "platform": platform.platform(),
        }
        try:
            async with TallyAdapter(
                base_url=self.config.tally_url,
                company=self.config.tally_company,
                timeout=min(self.config.tally_timeout_seconds, 10),
            ) as tally:
                company = await tally.open_company()
                state["tally_running"] = True
                state["company_open"] = company
                if self.config.require_company_match and self.config.tally_company and company:
                    state["period_open"] = (
                        self.config.tally_company.strip().lower() in company.strip().lower()
                        or company.strip().lower() in self.config.tally_company.strip().lower()
                    )
        except TallyUnavailable:
            state["tally_running"] = False
            state["period_open"] = False
        except Exception as exc:  # pragma: no cover - defensive
            state["tally_running"] = False
            state["error"] = f"{type(exc).__name__}"
        return state

    # ── supervisor ──────────────────────────────────────────────────────────

    async def run_forever(self, *, max_cycles: int | None = None) -> None:
        cycles = 0
        while not self._stop.is_set():
            try:
                summary = await self.cycle()
                log.info("cycle: %s", summary)
            except Exception as exc:  # pragma: no cover - the agent must not die
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.queue.log("error", "cycle failed", {"error": self.last_error})
                log.exception("cycle failed")
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.poll_seconds)
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()
