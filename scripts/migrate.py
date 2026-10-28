#!/usr/bin/env python
"""Migration runner for the control plane.

Applies every ``supabase/migrations/*.sql`` file in filename order, tracking what
has run in ``public.schema_migrations`` so re-runs are safe. Tenant schemas are
*not* created here: they are created per tenant from ``supabase/tenant_schema.sql``
by ``engine.db.provision.provision_tenant``.

Usage::

    python scripts/migrate.py            # apply pending migrations
    python scripts/migrate.py --status   # show what has been applied
    python scripts/migrate.py --dry-run  # print pending files without applying
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

import psycopg

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"

TRACKING_TABLE = """
create table if not exists public.schema_migrations (
  version    text primary key,
  checksum   text not null,
  applied_at timestamptz not null default now(),
  duration_ms integer
)
"""


def load_env(path: pathlib.Path) -> None:
    """Minimal .env loader so the script works without extra dependencies."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set (put it in .env)")
    return url


def migration_files() -> list[pathlib.Path]:
    return sorted(p for p in MIGRATIONS.glob("*.sql"))


def sha256_file(path: pathlib.Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def applied(cursor) -> dict[str, str]:
    cursor.execute("select version, checksum from public.schema_migrations")
    return {row[0]: row[1] for row in cursor.fetchall()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply control-plane migrations")
    parser.add_argument("--status", action="store_true", help="show applied migrations")
    parser.add_argument("--dry-run", action="store_true", help="do not write anything")
    args = parser.parse_args()

    load_env(ROOT / ".env")
    files = migration_files()
    if not files:
        sys.exit(f"no migrations found in {MIGRATIONS}")

    with psycopg.connect(database_url(), connect_timeout=20, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(TRACKING_TABLE)
            done = applied(cur)

        if args.status:
            for path in files:
                state = "applied " if path.name in done else "pending "
                print(f"{state}{path.name}")
            return 0

        pending = [p for p in files if p.name not in done]
        if not pending:
            print("database is up to date; nothing to apply")
            return 0

        for path in pending:
            checksum = sha256_file(path)
            if args.dry_run:
                print(f"would apply {path.name}")
                continue
            sql = path.read_text(encoding="utf-8")
            print(f"applying {path.name} ... ", end="", flush=True)
            import time

            started = time.perf_counter()
            with conn.cursor() as cur:
                cur.execute(sql)
                elapsed = int((time.perf_counter() - started) * 1000)
                cur.execute(
                    "insert into public.schema_migrations (version, checksum, duration_ms) "
                    "values (%s, %s, %s) on conflict (version) do update "
                    "set checksum = excluded.checksum, applied_at = now(), "
                    "duration_ms = excluded.duration_ms",
                    (path.name, checksum, elapsed),
                )
            print(f"ok ({elapsed} ms)")

            # A migration that changed previously-applied files is worth flagging.
        stale = [p.name for p in files if p.name in done and done[p.name] != sha256_file(p)]
        for name in stale:
            print(f"warning: {name} changed after it was applied", file=sys.stderr)

    print("migrations complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
