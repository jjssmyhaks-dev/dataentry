#!/usr/bin/env python
"""Create a tenant, its schema, its pack binding and its onboarding master data.

This is the onboarding path in miniature: what an implementation engineer does on
day one for a new customer (section 6.5a). It is idempotent, so re-running it is
safe.

    python scripts/seed_tenant.py --slug precision-pipes --name "Precision Pipes Limited"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from engine.db import Database, TenantRef, resolve_tenant  # noqa: E402
from engine.packs.loader import PackRegistry, bind_pack_to_tenant, sync_pack  # noqa: E402
from engine.posting.commands import generate_keypair  # noqa: E402
from engine.provisioning import provision_tenant  # noqa: E402
from engine.repo import tenant as tenant_repo  # noqa: E402
from engine.settings import get_settings  # noqa: E402


async def seed(
    slug: str,
    name: str,
    *,
    workos_org: str | None,
    posting_mode: str,
    autonomy_stage: int,
    kill_switch: bool,
    with_agent: bool,
) -> None:
    settings = get_settings()
    db = Database(settings)
    await db.open()
    try:
        registry = PackRegistry()
        pack = registry.get("manufacturing")
        tenant_row = await provision_tenant(
            db,
            slug=slug,
            name=name,
            workos_organization_id=workos_org,
            pack_key=pack.key,
            settings={
                "bank_ledger": "HDFC Bank - Current A/c 7890",
                "inventory_on": True,
                "e_invoicing_applies": False,
                "warns_allow_autopost": False,
                "reviewer_approval_limit": 250000,
                "connector": "tally_bridge",
            },
        )
        await sync_pack(db, pack, status="active", notes="seeded by scripts/seed_tenant.py")
        await bind_pack_to_tenant(db, tenant_row["id"], pack)

        tenant = await resolve_tenant(db, tenant_id=tenant_row["id"])
        assert tenant is not None

        # Update autonomy settings on the tenant we just created.
        async with db.control() as cur:
            await cur.execute(
                "update public.tenants set posting_mode = %s, autonomy_stage = %s, kill_switch = %s "
                "where id = %s",
                (posting_mode, autonomy_stage, kill_switch, tenant.id),
            )
            await cur.execute(
                """
                insert into public.tenant_members
                  (tenant_id, workos_user_id, email, full_name, role, approval_limit)
                values (%s, 'user_01HDEVREVIEWER', 'reviewer@example.com', 'Dev Reviewer', 'owner', 250000)
                on conflict (tenant_id, workos_user_id) do nothing
                """,
                (tenant.id,),
            )

        # Onboarding master data: tax ledgers and supplier ledger mappings read
        # from Tally in the real world, entered here so the pipeline can run.
        async with db.tenant(tenant) as cur:
            await tenant_repo.upsert_ledger_mapping(
                cur,
                gstin=None,
                name="__tenant_defaults__",
                cgst_ledger="Input CGST 18%",
                sgst_ledger="Input SGST 18%",
                igst_ledger="Input IGST 18%",
                roundoff_ledger="Round Off",
                tds_ledger="TDS Payable",
                confidence=1.0,
                source="manual",
                approved_by="onboarding",
            )
            for supplier, gstin, party_ledger, purchase_ledger in SUPPLIERS:
                await tenant_repo.upsert_ledger_mapping(
                    cur,
                    gstin=gstin,
                    name=supplier,
                    target_party_ledger=party_ledger,
                    purchase_ledger=purchase_ledger,
                    expense_ledger=None,
                    confidence=1.0,
                    source="manual",
                    approved_by="onboarding",
                )
            await cur.execute(
                """
                insert into master_snapshots (kind, company, item_count, payload)
                values ('periods', 'PRECISION PIPES LIMITED 2026-27', 1, %s)
                """,
                (
                    json.dumps(
                        [
                            {"from": "2026-04-01", "to": "2027-03-31", "label": "FY 2026-27"},
                        ]
                    ),
                ),
            )
            await cur.execute(
                """
                insert into master_snapshots (kind, company, item_count, payload)
                values ('ledgers', 'PRECISION PIPES LIMITED 2026-27', 8, %s)
                """,
                (
                    json.dumps(
                        [
                            {"name": "Input CGST 18%", "group": "Duties & Taxes"},
                            {"name": "Input SGST 18%", "group": "Duties & Taxes"},
                            {"name": "Input IGST 18%", "group": "Duties & Taxes"},
                            {"name": "Round Off", "group": "Indirect Expenses"},
                            {"name": "TDS Payable", "group": "Duties & Taxes"},
                            {"name": "Purchase - Steel", "group": "Purchase Accounts"},
                            {"name": "Purchase - Plastics", "group": "Purchase Accounts"},
                            {"name": "HDFC Bank - Current A/c 7890", "group": "Bank Accounts"},
                        ]
                    ),
                ),
            )

        if with_agent:
            credential_id = f"agent-{slug}-pc1"
            async with db.control() as cur:
                await cur.execute(
                    "select id, public_key from public.agents where credential_id = %s",
                    (credential_id,),
                )
                existing_agent = await cur.fetchone()
            if existing_agent:
                print(f"agent already exists: {credential_id}")
            else:
                private_key, public_key = generate_keypair()
                async with db.control() as cur:
                    await cur.execute(
                        """
                        insert into public.agents
                          (tenant_id, name, credential_id, public_key, tally_company, mode, status)
                        values (%s, %s, %s, %s, %s, 'draft', 'active')
                        """,
                        (
                            tenant.id,
                            f"{slug}-pc-1",
                            credential_id,
                            public_key,
                            "PRECISION PIPES LIMITED 2026-27",
                        ),
                    )
                print(f"agent credential id: {credential_id}")
                print(f"AGENT_COMMAND_SIGNING_KEY={private_key}")
                print(f"AGENT_COMMAND_PUBLIC_KEY={public_key}")
                print(
                    "-> put both in .env (the private one is the cloud's signing key) and give "
                    "the public key to the agent for verification"
                )

        print(f"tenant ready: {slug} (schema {tenant.schema}) - {tenant.id}")
        print(f"workos organisation: {workos_org or 'not set (dev session will use the slug)'}")
        print(f"posting mode: {posting_mode}, autonomy stage: {autonomy_stage}")
    finally:
        await db.close()


SUPPLIERS = [
    ("SHREE STEEL TUBES PVT LTD", "27AAPFU0939F1ZV", "Shree Steel Tubes Pvt Ltd", "Purchase - Steel"),
    ("GUJARAT POLYMER INDUSTRIES", "24AABCS1429B1Z0", "Gujarat Polymer Industries", "Purchase - Plastics"),
    ("KARNATAKA FASTENERS PVT LTD", "29AABCK7642P1ZU", "Karnataka Fasteners Pvt Ltd", "Purchase - Fasteners"),
    ("NAGPUR MACHINING WORKS", "27AABCT1332L1ZE", "Nagpur Machining Works", "Purchase - Job Work"),
    ("NAGPUR ENGINEERING CONSULTANTS", "27AAGCN2211L1ZJ", "Nagpur Engineering Consultants", "Professional Fees"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a tenant and its onboarding data")
    parser.add_argument("--slug", default="precision-pipes")
    parser.add_argument("--name", default="Precision Pipes Limited")
    parser.add_argument("--workos-org", default=None, help="WorkOS organisation id")
    parser.add_argument("--posting-mode", default="draft", choices=["shadow", "draft", "autonomous"])
    parser.add_argument("--autonomy-stage", default=0, type=int)
    parser.add_argument("--kill-switch", action="store_true", default=True)
    parser.add_argument("--no-kill-switch", dest="kill_switch", action="store_false")
    parser.add_argument("--with-agent", action="store_true", default=True)
    parser.add_argument("--no-agent", dest="with_agent", action="store_false")
    args = parser.parse_args()

    asyncio.run(
        seed(
            args.slug,
            args.name,
            workos_org=args.workos_org,
            posting_mode=args.posting_mode,
            autonomy_stage=args.autonomy_stage,
            kill_switch=args.kill_switch,
            with_agent=args.with_agent,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
