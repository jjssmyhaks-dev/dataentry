#!/usr/bin/env python
"""Inspect the control plane and per-tenant schemas.

    python scripts/db_inspect.py                 # control-plane summary
    python scripts/db_inspect.py --tenant <slug>  # tables + row counts in a tenant schema
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from engine.settings import get_settings  # noqa: E402

import psycopg  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", help="tenant slug to inspect")
    args = parser.parse_args()

    settings = get_settings()
    with psycopg.connect(settings.database_url, connect_timeout=20) as conn, conn.cursor() as cur:
        cur.execute("select version from public.schema_migrations order by version")
        print("migrations:", ", ".join(r[0] for r in cur.fetchall()))

        cur.execute("select count(*) from public.packs")
        print("packs:", cur.fetchone()[0])
        cur.execute("select pack_key, version, status from public.pack_versions order by pack_key, version")
        rows = cur.fetchall()
        print("pack versions:", rows or "(none synced yet)")

        cur.execute("select slug, name, schema_name, posting_mode, autonomy_stage, kill_switch from public.tenants order by created_at")
        tenants = cur.fetchall()
        print(f"tenants: {len(tenants)}")
        for t in tenants:
            print(f"  - {t[0]:<20} schema={t[2]:<24} mode={t[3]:<11} stage={t[4]} kill_switch={t[5]}")

        if args.tenant:
            cur.execute("select schema_name from public.tenants where slug = %s", (args.tenant,))
            row = cur.fetchone()
            if not row:
                sys.exit(f"unknown tenant slug: {args.tenant}")
            schema = row[0]
            cur.execute(
                "select table_name from information_schema.tables "
                "where table_schema = %s order by table_name",
                (schema,),
            )
            tables = [r[0] for r in cur.fetchall()]
            print(f"\n{schema}: {len(tables)} tables")
            for table in tables:
                cur.execute(f'select count(*) from "{schema}"."{table}"')
                print(f"  {table:<28} {cur.fetchone()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
