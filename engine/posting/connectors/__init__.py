"""Connector registry and dispatch (section 6.5).

The pipeline asks for a connector by name, or lets the tenant's settings decide.
Nothing else in the engine needs to know which destination is in play.
"""

from __future__ import annotations

import psycopg

from engine.db import TenantRef
from engine.posting.connectors.base import Connector, PostContext, PostResult  # noqa: F401
from engine.posting.connectors.other import CsvConnector, WebhookConnector
from engine.posting.connectors.tally import (
    ShadowConnector,
    TallyBridgeConnector,
    TallyFileConnector,
)

TARGETS: dict[str, type] = {
    "tally_bridge": TallyBridgeConnector,
    "tallyprime_bridge": TallyBridgeConnector,
    "tally_file": TallyFileConnector,
    "tallyprime_file": TallyFileConnector,
    "csv": CsvConnector,
    "webhook": WebhookConnector,
    "shadow": ShadowConnector,
}

DEFAULT_TARGET = "tally_bridge"


def connector_names() -> list[str]:
    return sorted(TARGETS)


def get_connector(target: str, *, cur: psycopg.Cursor | None = None) -> Connector:
    try:
        factory = TARGETS[target]
    except KeyError:
        raise ValueError(
            f"unknown connector {target!r}; known connectors: {', '.join(connector_names())}"
        ) from None
    if factory in {TallyBridgeConnector, ShadowConnector}:
        return factory(cur=cur)
    return factory()


def choose_target(tenant: TenantRef, *, mode: str, agent_available: bool) -> str:
    """Which destination this tenant should use.

    The tenant's setting wins; otherwise an online agent means the bridge, and no
    agent means the file import. Autonomous posting without an agent is not
    possible, so it falls back rather than failing.
    """
    configured = (tenant.settings or {}).get("connector")
    if configured:
        return configured
    if mode == "shadow":
        return "shadow"
    if agent_available:
        return "tally_bridge"
    return "tally_file"
