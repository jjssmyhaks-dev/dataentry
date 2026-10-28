"""Tenant provisioning.

One WorkOS organisation maps to one tenant and one Postgres schema (section 12c).
The schema is created from ``supabase/tenant_schema.sql``, which is the single
source of truth for a tenant's tables, so provisioning and the schema cannot drift
apart.
"""

from __future__ import annotations

import pathlib
import re
import uuid
from typing import Any

import psycopg

from engine.db import Database, J, schema_name_for
from engine.settings import ROOT, get_settings

TEMPLATE = ROOT / "supabase" / "tenant_schema.sql"


async def provision_tenant(
    db: Database,
    *,
    slug: str,
    name: str,
    industry: str = "manufacturing",
    workos_organization_id: str | None = None,
    pack_key: str = "manufacturing",
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the tenant row and its schema, and return the tenant row."""
    schema = schema_name_for(slug)
    if not re.match(r"^tenant_[a-z0-9_]+$", schema):
        raise ValueError(f"refusing to create schema {schema!r}")

    sql = TEMPLATE.read_text(encoding="utf-8").replace("__SCHEMA__", schema)

    async with db.control() as cur:
        await cur.execute("select id from public.tenants where slug = %s", (slug,))
        existing = await cur.fetchone()
        if existing:
            return await _fetch(cur, existing["id"])

        await cur.execute(f'create schema if not exists "{schema}"')
        await cur.execute(sql)

        await cur.execute(
            """
            insert into public.tenants
              (slug, name, industry, workos_organization_id, schema_name, pack_key, settings,
               posting_mode, autonomy_stage, kill_switch)
            values (%s, %s, %s, %s, %s, %s, %s, 'draft', 0, true)
            on conflict (slug) do update
              set name = excluded.name,
                  workos_organization_id = coalesce(excluded.workos_organization_id,
                                                    public.tenants.workos_organization_id),
                  schema_name = excluded.schema_name
            returning *
            """,
            (slug, name, industry, workos_organization_id, schema, pack_key, J(settings or {})),
        )
        tenant = await cur.fetchone()
        await cur.execute(
            """
            insert into public.activity_log (tenant_id, actor_type, actor_id, action, detail)
            values (%s, 'system', 'provisioner', 'tenant.provisioned', %s)
            """,
            (tenant["id"], J({"schema": schema, "pack_key": pack_key})),
        )
    return tenant


async def _fetch(cur: psycopg.AsyncCursor, tenant_id: uuid.UUID) -> dict[str, Any]:
    await cur.execute("select * from public.tenants where id = %s", (tenant_id,))
    return await cur.fetchone()


def tenant_schema_tables() -> list[str]:
    sql = TEMPLATE.read_text(encoding="utf-8")
    return sorted(set(re.findall(r"create table if not exists __SCHEMA__\.(\w+)", sql)))


def settings_path() -> pathlib.Path:
    return get_settings().packs_dir
