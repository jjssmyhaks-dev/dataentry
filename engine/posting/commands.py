"""Signed commands for the bridge agent (Appendix E).

The cloud holds the queue of vouchers to post; each command is signed so that a
stolen connection cannot post vouchers. Commands expire rather than posting late,
and an expired command returns its record to the review queue.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from engine.db import J
from engine.settings import get_settings


class SigningNotConfigured(RuntimeError):
    """Raised when the agent signing keypair has not been generated yet."""


# ── key material ────────────────────────────────────────────────────────────


def generate_keypair() -> tuple[str, str]:
    """Returns (private_b64, public_b64) for the AGENT_COMMAND_* settings."""
    private = Ed25519PrivateKey.generate()
    private_bytes = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return (
        base64.b64encode(private_bytes).decode(),
        base64.b64encode(public_bytes).decode(),
    )


def _load_private(key: str | None = None) -> Ed25519PrivateKey:
    settings = get_settings()
    material = key or settings.agent_command_signing_key
    if not material:
        raise SigningNotConfigured(
            "AGENT_COMMAND_SIGNING_KEY is not set; run `python -m bridge_agent.cli keygen` "
            "and put the pair in .env"
        )
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(material))


def _load_public(key: str | None = None) -> Ed25519PublicKey:
    settings = get_settings()
    material = key or settings.agent_command_public_key
    if not material:
        raise SigningNotConfigured("AGENT_COMMAND_PUBLIC_KEY is not set")
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(material))


def canonical_payload(payload: dict[str, Any]) -> bytes:
    """Deterministic bytes so the agent can verify what the cloud signed."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


def sign_payload(payload: dict[str, Any], *, key: str | None = None) -> str:
    signature = _load_private(key).sign(canonical_payload(payload))
    return base64.b64encode(signature).decode()


def verify_signature(payload: dict[str, Any], signature: str, *, key: str | None = None) -> bool:
    try:
        _load_public(key).verify(base64.b64decode(signature), canonical_payload(payload))
    except (InvalidSignature, ValueError):
        return False
    return True


# ── the queue ───────────────────────────────────────────────────────────────


async def create_command(
    cur: psycopg.Cursor,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    kind: str,
    idempotency_key: str,
    payload: dict[str, Any],
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Queue a command for an agent, signed and stamped with an expiry."""
    settings = get_settings()
    ttl = ttl_seconds or settings.agent_command_ttl_seconds
    envelope = {
        **payload,
        "command_kind": kind,
        "idempotency_key": idempotency_key,
        "tenant_id": str(tenant_id),
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat(),
        "command_id": str(uuid.uuid4()),
    }

    try:
        envelope["signature"] = sign_payload(envelope)
    except SigningNotConfigured:
        # The queue entry is still written so the record is visible and the team
        # can see why nothing posted, rather than losing the command.
        envelope["signature"] = None

    await cur.execute(
        """
        insert into public.agent_commands
          (id, tenant_id, agent_id, kind, idempotency_key, payload, signature, expires_at)
        values (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))
        on conflict (idempotency_key) do update
          set payload = excluded.payload, signature = excluded.signature,
              status = case when public.agent_commands.status in ('failed', 'expired')
                            then 'queued' else public.agent_commands.status end,
              expires_at = excluded.expires_at
        returning *
        """,
        (
            uuid.UUID(envelope["command_id"]),
            tenant_id,
            agent_id,
            kind,
            idempotency_key,
            J(envelope),
            envelope["signature"],
            ttl,
        ),
    )
    return await cur.fetchone()


async def expire_stale_commands(cur: psycopg.Cursor, tenant_id: uuid.UUID) -> int:
    await cur.execute(
        """
        update public.agent_commands
           set status = 'expired', error_text = 'command expired before it was delivered'
         where tenant_id = %s and status = 'queued' and expires_at < now()
        """,
        (tenant_id,),
    )
    return cur.rowcount


async def pending_commands(
    cur: psycopg.Cursor, tenant_id: uuid.UUID, *, agent_id: uuid.UUID | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    await cur.execute(
        """
        select * from public.agent_commands
         where tenant_id = %s
           and status = 'queued'
           and expires_at > now()
           and (%s::uuid is null or agent_id = %s::uuid)
         order by created_at
         limit %s
        """,
        (tenant_id, agent_id, agent_id, limit),
    )
    return list(await cur.fetchall())


async def record_command_result(
    cur: psycopg.Cursor,
    command_id: uuid.UUID,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error_text: str | None = None,
) -> None:
    await cur.execute(
        """
        update public.agent_commands
           set status = %s, result = %s, error_text = %s,
               acked_at = case when %s in ('acked', 'failed') then now() else acked_at end,
               attempts = attempts + 1
         where id = %s
        """,
        (status, J(result or {}), error_text, status, command_id),
    )


async def active_agent(cur: psycopg.Cursor, tenant_id: uuid.UUID) -> dict[str, Any] | None:
    await cur.execute(
        """
        select * from public.agents
         where tenant_id = %s and status = 'active'
         order by last_heartbeat_at desc nulls last
         limit 1
        """,
        (tenant_id,),
    )
    return await cur.fetchone()
