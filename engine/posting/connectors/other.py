"""CSV/Excel export and webhook-out connectors (section 6.5)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from engine.observability import get_tracer
from engine.posting.commands import sign_payload
from engine.posting.connectors.base import PostContext, PostResult, basic_validation
from engine.posting.connectors.tally import draft_to_csv_rows, rows_to_csv


class CsvConnector:
    """Every validated record can be exported, with no destination at all."""

    name = "csv"
    label = "CSV / Excel export"

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
                require_human=True,
            )
        rows = draft_to_csv_rows(ctx.draft)
        return PostResult(
            ok=True,
            status="staged",
            connector=self.name,
            response={
                "format": "csv",
                "file_name": f"{ctx.draft.idempotency_key.replace(':', '_')}.csv",
                "csv": rows_to_csv(rows),
                "rows": rows,
            },
            require_human=True,
        )

    async def confirm(self, ctx: PostContext, result: PostResult) -> PostResult:
        return result

    async def roll_back(self, ctx: PostContext, result: PostResult) -> PostResult:
        return PostResult(
            ok=True, status="cancelled", connector=self.name, response={"note": "export withdrawn"}
        )


class WebhookConnector:
    """Posts a signed JSON payload to the tenant's endpoint, for their own systems."""

    name = "webhook"
    label = "Webhook out"

    async def validate(self, ctx: PostContext) -> list[str]:
        problems = basic_validation(ctx)
        url = (ctx.tenant.settings or {}).get("webhook_url")
        if not url:
            problems.append(
                "No webhook URL is configured for this tenant (Settings, webhook_url)."
            )
        return problems

    async def post(self, ctx: PostContext) -> PostResult:
        problems = await self.validate(ctx)
        if problems:
            return PostResult(
                ok=False,
                status="tally_rejected",
                connector=self.name,
                error_text=" ".join(problems),
                require_human=True,
            )
        payload: dict[str, Any] = {
            "event": "voucher.ready",
            "tenant": ctx.tenant.slug,
            "idempotency_key": ctx.draft.idempotency_key,
            "voucher": ctx.draft.model_dump(mode="json"),
        }
        if not (ctx.tenant.settings or {}).get("webhook_signing_disabled"):
            try:
                payload["signature"] = sign_payload(payload)
            except Exception:
                payload["signature"] = None

        url = ctx.tenant.settings["webhook_url"]
        with get_tracer().span("webhook.post", url=url) as span:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.post(url, json=payload)
                span.update(status_code=response.status_code)
                if response.status_code >= 400:
                    return PostResult(
                        ok=False,
                        status="transport_error",
                        connector=self.name,
                        response={"status_code": response.status_code, "body": response.text[:500]},
                        error_text=f"the webhook returned HTTP {response.status_code}",
                        require_human=True,
                    )
                return PostResult(
                    ok=True,
                    status="posted",
                    connector=self.name,
                    response={"status_code": response.status_code, "body": response.text[:500]},
                )
            except httpx.HTTPError as exc:
                span.update(error=str(exc))
                return PostResult(
                    ok=False,
                    status="transport_error",
                    connector=self.name,
                    error_text=f"the webhook could not be reached: {type(exc).__name__}",
                    require_human=False,
                    retry_after_seconds=60,
                )

    async def confirm(self, ctx: PostContext, result: PostResult) -> PostResult:
        return result

    async def roll_back(self, ctx: PostContext, result: PostResult) -> PostResult:
        payload = {
            "event": "voucher.reversed",
            "tenant": ctx.tenant.slug,
            "idempotency_key": ctx.draft.idempotency_key,
        }
        url = (ctx.tenant.settings or {}).get("webhook_url")
        if not url:
            return PostResult(
                ok=False, status="cancelled", connector=self.name, error_text="no webhook URL"
            )
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(url, json=payload)
            return PostResult(
                ok=response.status_code < 400,
                status="cancelled",
                connector=self.name,
                response={"status_code": response.status_code},
            )
        except httpx.HTTPError as exc:
            return PostResult(
                ok=False,
                status="transport_error",
                connector=self.name,
                error_text=f"the reversal notice failed: {type(exc).__name__}",
            )


def webhook_payload_preview(ctx: PostContext) -> str:
    return json.dumps(
        {"event": "voucher.ready", "idempotency_key": ctx.draft.idempotency_key},
        indent=2,
    )
