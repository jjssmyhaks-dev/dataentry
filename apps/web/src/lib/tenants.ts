import { env } from "./env";
import type { Session } from "./session";

/**
 * Organisation → tenant provisioning (section 12c).
 *
 * One WorkOS organisation maps to exactly one tenant and one Postgres schema.
 * The mapping is created the first time somebody from that organisation signs
 * in, so a new customer never lands on a 403 or in another customer's data. The
 * engine keys this on the organisation id, which comes from the signed WorkOS
 * session and never from the browser.
 */

export interface Tenant {
  id: string;
  slug: string;
  name: string;
  schema_name: string;
  workos_organization_id: string | null;
  pack_key: string;
  posting_mode: string;
  autonomy_stage: number;
  status: string;
}

export interface ProvisionResult {
  created: boolean;
  tenant: Tenant;
  pack_ready?: boolean;
}

/** A slug we would accept back from the engine, used only for the audit line. */
export function tenantLabel(tenant: Tenant | null): string {
  if (!tenant) return "no tenant";
  return `${tenant.name} (${tenant.slug})`;
}

export async function ensureTenant(session: Session): Promise<ProvisionResult> {
  if (!session.organizationId) {
    throw new Error(
      "This account is not a member of a WorkOS organisation, so there is no tenant to open.",
    );
  }

  const response = await fetch(`${env.engineUrl}/v1/tenants/provision`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${env.engineToken}`,
    },
    body: JSON.stringify({
      workos_organization_id: session.organizationId,
      name: session.organizationName ?? session.organizationId,
    }),
    cache: "no-store",
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(
      `The engine could not provision a tenant for this organisation (${response.status}): ${detail.slice(0, 200)}`,
    );
  }

  return (await response.json()) as ProvisionResult;
}
