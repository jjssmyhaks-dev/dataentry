"""Authentication and tenant resolution for the engine API.

Three kinds of caller, three credentials (sections 8 and 12c):

* **The web app** holds the WorkOS session cookie, verifies it, and calls the
  engine with its service token plus the *organisation id from that session*. The
  engine resolves the organisation to exactly one tenant and one schema. The
  tenant is never taken from anything the browser sends.
* **A calling Ygudy product** (Counsel, the CA product, the admissions SaaS)
  authenticates with its own service credential and passes the tenant it owns.
* **The bridge agent** is a machine, not a user: it presents its credential id and
  signs its payloads.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from fastapi import Header, HTTPException, Request, status

from engine.db import Database, TenantRef, resolve_tenant
from engine.settings import Settings, get_settings

PrincipalKind = Literal["user", "product", "agent", "system"]


@dataclass
class Principal:
    kind: PrincipalKind
    actor: str
    role: str
    tenant: TenantRef
    email: str | None = None
    agent_id: uuid.UUID | None = None

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    def require_role(self, *roles: str) -> None:
        if self.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"this action needs one of these roles: {', '.join(roles)}",
            )


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_settings_dep() -> Settings:
    return get_settings()


async def _tenant_from_organization(db: Database, organization_id: str) -> TenantRef:
    tenant = await resolve_tenant(db, workos_organization_id=organization_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="no tenant is mapped to this organisation",
        )
    if tenant.status in {"suspended", "churned"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"tenant is {tenant.status}"
        )
    return tenant


async def user_principal(
    db: Database,
    settings: Settings,
    *,
    authorization: str | None,
    organization_id: str | None,
    actor_id: str | None,
    actor_role: str | None,
    actor_email: str | None,
    dev_tenant_slug: str | None,
) -> Principal:
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token or token != settings.engine_service_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="the engine service token is required for this call",
        )

    if not organization_id:
        # Local development without WorkOS keys, so the review queue can be
        # exercised. Never enabled in production.
        if settings.engine_env == "production" or not dev_tenant_slug:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="the WorkOS organisation of the signed session is required",
            )
        tenant = await resolve_tenant(db, slug=dev_tenant_slug)
        if tenant is None:
            raise HTTPException(status_code=404, detail=f"unknown tenant slug {dev_tenant_slug}")
        return Principal(
            kind="user",
            actor=actor_id or "dev-user",
            role=actor_role or "owner",
            tenant=tenant,
            email=actor_email,
        )

    tenant = await _tenant_from_organization(db, organization_id)
    return Principal(
        kind="user",
        actor=actor_id or "unknown-user",
        role=actor_role or "viewer",
        tenant=tenant,
        email=actor_email,
    )


async def request_principal(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    organization_id: str | None = Header(default=None, alias="X-WorkOS-Organization"),
    actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
    actor_role: str | None = Header(default=None, alias="X-Actor-Role"),
    actor_email: str | None = Header(default=None, alias="X-Actor-Email"),
    dev_tenant: str | None = Header(default=None, alias="X-Dev-Tenant"),
    product_credential: str | None = Header(default=None, alias="X-Product-Credential"),
    product_name: str | None = Header(default=None, alias="X-Product"),
    product_tenant: str | None = Header(default=None, alias="X-Tenant-Id"),
    agent_credential: str | None = Header(default=None, alias="X-Agent-Credential-Id"),
) -> Principal:
    db: Database = request.app.state.db
    settings: Settings = get_settings()

    if agent_credential:
        return await agent_principal(db, agent_credential)
    if product_credential:
        return await product_principal(db, settings, product_credential, product_name, product_tenant)
    return await user_principal(
        db,
        settings,
        authorization=authorization,
        organization_id=organization_id,
        actor_id=actor_id,
        actor_role=actor_role,
        actor_email=actor_email,
        dev_tenant_slug=dev_tenant,
    )


async def product_principal(
    db: Database,
    settings: Settings,
    credential: str,
    product: str | None,
    tenant_id: str | None,
) -> Principal:
    match = next(
        (name for name, secret in settings.engine_product_credentials.items() if secret == credential),
        None,
    )
    if match is None:
        raise HTTPException(status_code=401, detail="unknown product credential")
    if not tenant_id:
        raise HTTPException(
            status_code=400,
            detail="calling products must pass the tenant they are acting for (X-Tenant-Id)",
        )
    try:
        tenant_uuid = uuid.UUID(tenant_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="X-Tenant-Id must be a tenant id") from None
    tenant = await resolve_tenant(db, tenant_id=tenant_uuid)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return Principal(kind="product", actor=product or match, role="product", tenant=tenant)


async def agent_principal(db: Database, credential_id: str) -> Principal:
    async with db.control() as cur:
        await cur.execute(
            "select * from public.agents where credential_id = %s", (credential_id,)
        )
        agent = await cur.fetchone()
    if agent is None:
        raise HTTPException(status_code=401, detail="unknown agent credential")
    if agent["status"] == "revoked":
        raise HTTPException(status_code=403, detail="this agent credential has been revoked")
    tenant = await resolve_tenant(db, tenant_id=agent["tenant_id"])
    if tenant is None:
        raise HTTPException(status_code=403, detail="the agent's tenant is not available")
    return Principal(
        kind="agent",
        actor=str(agent["id"]),
        role="agent",
        tenant=tenant,
        agent_id=agent["id"],
    )
