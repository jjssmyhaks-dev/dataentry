"""The agent's outbound connection to the cloud.

Only outbound: the agent dials the cloud and the cloud's commands travel back on
that same connection, so no inbound port is opened at the customer's site.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class CloudUnavailable(RuntimeError):
    pass


@dataclass
class CloudClient:
    base_url: str
    credential_id: str
    timeout: int = 30

    def _headers(self) -> dict[str, str]:
        return {"X-Agent-Credential-Id": self.credential_id}

    async def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(
                    method, url, headers=self._headers(), json=json_body
                )
        except httpx.HTTPError as exc:
            raise CloudUnavailable(f"the cloud could not be reached: {type(exc).__name__}") from exc
        if response.status_code == 401:
            raise CloudUnavailable(
                "the cloud rejected this agent credential; it may have been revoked"
            )
        if response.status_code >= 400:
            raise CloudUnavailable(
                f"the cloud answered HTTP {response.status_code}: {response.text[:200]}"
            )
        return response.json() if response.content else {}

    async def hello(self) -> dict[str, Any]:
        return await self._request("POST", "/v1/agent/hello")

    async def heartbeat(self, state: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v1/agent/heartbeat", json_body=state)

    async def commands(self, limit: int = 20) -> list[dict[str, Any]]:
        payload = await self._request("GET", f"/v1/agent/commands?limit={limit}")
        return payload.get("commands", [])

    async def report_result(
        self,
        command_id: str,
        *,
        status: str,
        result: dict[str, Any],
        error_text: str | None = None,
        customer_payload: dict[str, Any] | None = None,
        signature: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/v1/agent/commands/{command_id}/result",
            json_body={
                "status": status,
                "result": result,
                "error_text": error_text,
                "payload": customer_payload or {},
                "signature": signature,
            },
        )

    async def sync_masters(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v1/agent/masters", json_body=payload)
