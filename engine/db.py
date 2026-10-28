"""Database access.

Two rules, enforced here rather than trusted to callers:

1. The tenant schema is chosen **on the server** from the tenant resolved out of
   the signed session (section 12c). ``engine.tenant_id`` is set on the
   connection for every tenant query, so the ``tenant_id`` column default and the
   row-level security policy both agree with the schema we selected.
2. Schema names are validated against the pattern we generate, so nothing that
   arrives over the wire can be interpolated into a ``SET search_path``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, AsyncIterator, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb, set_json_dumps
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from engine.settings import Settings, get_settings

# psycopg's async I/O waits on a socket, which needs an event loop that supports
# add_reader/add_writer. Windows defaults to ProactorEventLoop, which does not, so
# the pool would never finish opening. Setting the policy here means any entry
# point that imports this module before creating its loop gets the right one; for
# uvicorn, start the engine with `python -m engine.api.server`.
if sys.platform == "win32":  # pragma: no cover - platform specific
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _json_default(value: Any) -> Any:
    """Extracted payloads carry dates, Decimals and UUIDs; jsonb needs text."""
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False)


# Applied process-wide for anything created after this import.
set_json_dumps(_json_dumps)


def J(value: Any) -> Jsonb:
    """A jsonb parameter.

    The serialiser is passed on every call rather than relied on globally:
    psycopg's dumper captures the global ``dumps`` when the adapter is built, so a
    later ``set_json_dumps`` does not reach an already-built adapter. Passing it
    here is explicit and cannot drift.
    """
    return Jsonb(value if value is not None else {}, dumps=_json_dumps)

SCHEMA_RE = re.compile(r"^tenant_[a-z0-9_]{1,48}$")

JsonbValue = Jsonb


def schema_name_for(slug: str) -> str:
    """Deterministic schema name for a tenant slug."""
    clean = re.sub(r"[^a-z0-9]+", "_", slug.lower()).strip("_")
    if not clean:
        raise ValueError("tenant slug must contain at least one alphanumeric character")
    return f"tenant_{clean[:48]}"


def assert_schema_name(schema: str) -> str:
    if not SCHEMA_RE.match(schema):
        raise ValueError(f"refusing to use unexpected schema name: {schema!r}")
    return schema


@dataclass(frozen=True)
class TenantRef:
    """A tenant resolved from a signed session, plus its autonomy settings."""

    id: uuid.UUID
    slug: str
    name: str
    schema: str
    pack_key: str
    posting_mode: str
    autonomy_stage: int
    kill_switch: bool
    confidence_threshold: float
    autopost_value_cap: Decimal
    daily_autopost_cap: int
    error_rate_limit: Decimal
    status: str = "active"
    auto_demote_on_breach: bool = True
    # Tenant settings carry things the pack does not fix: the bank ledger name,
    # whether warns may still auto-post, whether e-invoicing applies, and so on.
    settings: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_posting_mode(self) -> str:
        """The kill switch always wins over the configured mode (Appendix C)."""
        if self.kill_switch:
            return "draft" if self.posting_mode == "autonomous" else self.posting_mode
        return self.posting_mode

    @property
    def e_invoicing_applies(self) -> bool:
        return bool(self.settings.get("e_invoicing_applies", False))

    @property
    def warns_allow_autopost(self) -> bool:
        return bool(self.settings.get("warns_allow_autopost", False))

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> TenantRef:
        return cls(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            schema=assert_schema_name(row["schema_name"]),
            pack_key=row["pack_key"],
            posting_mode=row["posting_mode"],
            autonomy_stage=row["autonomy_stage"],
            kill_switch=row["kill_switch"],
            confidence_threshold=float(row["confidence_threshold"]),
            autopost_value_cap=Decimal(row["autopost_value_cap"]),
            daily_autopost_cap=row["daily_autopost_cap"],
            error_rate_limit=Decimal(row["error_rate_limit"]),
            status=row["status"],
            auto_demote_on_breach=bool(row.get("auto_demote_on_breach", True)),
            settings=dict(row.get("settings") or {}),
        )


class Database:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._pool: AsyncConnectionPool | None = None

    async def open(self) -> None:
        if self._pool is None:
            self._pool = AsyncConnectionPool(
                self.settings.database_url,
                min_size=1,
                max_size=8,
                open=False,
                kwargs={"row_factory": dict_row, "connect_timeout": 20},
            )
            await self._pool.open(wait=True, timeout=30)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _require_pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            raise RuntimeError("Database.open() has not been called")
        return self._pool

    @contextlib.asynccontextmanager
    async def control(self) -> AsyncIterator[psycopg.AsyncCursor]:
        """Cursor over the control plane (``public``)."""
        pool = self._require_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                yield cur

    @contextlib.asynccontextmanager
    async def tenant(self, tenant: TenantRef) -> AsyncIterator[psycopg.AsyncCursor]:
        """Cursor scoped to one tenant's schema, in one transaction."""
        pool = self._require_pool()
        schema = assert_schema_name(tenant.schema)
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute("select engine.set_tenant(%s)", (tenant.id,))
                    # `set local` is valid because we are inside a transaction
                    # block; the schema name is validated above.
                    await cur.execute(f"set local search_path to {schema}, public, extensions")
                    yield cur

    # ── synchronous helpers for migrations / scripts ────────────────────────

    def sync_connection(self, *, autocommit: bool = False) -> psycopg.Connection:
        return psycopg.connect(
            self.settings.database_url,
            autocommit=autocommit,
            row_factory=dict_row,
            connect_timeout=20,
        )


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db


# ── tenant resolution ───────────────────────────────────────────────────────


async def resolve_tenant(
    db: Database,
    *,
    tenant_id: uuid.UUID | None = None,
    slug: str | None = None,
    workos_organization_id: str | None = None,
) -> TenantRef | None:
    """Look a tenant up from the control plane. Never from a browser-supplied
    schema name."""
    clauses, params = [], []
    if tenant_id is not None:
        clauses.append("id = %s")
        params.append(tenant_id)
    if slug is not None:
        clauses.append("slug = %s")
        params.append(slug)
    if workos_organization_id is not None:
        clauses.append("workos_organization_id = %s")
        params.append(workos_organization_id)
    if not clauses:
        return None

    async with db.control() as cur:
        await cur.execute(
            f"select * from public.tenants where {' and '.join(clauses)} limit 1", params
        )
        row = await cur.fetchone()
    return TenantRef.from_row(row) if row else None


async def record_activity(
    cur: psycopg.Cursor,
    *,
    tenant_id: uuid.UUID | None,
    action: str,
    actor_type: str = "system",
    actor_id: str | None = None,
    actor_email: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    severity: str = "info",
) -> None:
    """Every action records the user, the tenant and the time (section 12c)."""
    await cur.execute(
        """
        insert into public.activity_log
          (tenant_id, actor_type, actor_id, actor_email, action,
           target_type, target_id, detail, severity)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            tenant_id,
            actor_type,
            actor_id,
            actor_email,
            action,
            target_type,
            target_id,
            Jsonb(detail or {}),
            severity,
        ),
    )


def sync_iter(sql: str, params: tuple[Any, ...] = ()) -> Iterator[dict[str, Any]]:
    """Small sync helper for scripts and tests."""
    with Database().sync_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        yield from cur.fetchall()
