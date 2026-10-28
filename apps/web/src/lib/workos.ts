import { WorkOS } from "@workos-inc/node";
import { env, workosConfigured } from "./env";
import { normaliseRole, type Role, type Session } from "./session";

/**
 * WorkOS owns login, organisations, roles and SSO (section 12c). Each WorkOS
 * organisation maps to exactly one tenant, and therefore one Postgres schema.
 */

let client: WorkOS | null = null;

export function workos(): WorkOS {
  if (!workosConfigured) throw new Error("WorkOS is not configured");
  if (!client) {
    client = new WorkOS(env.workosApiKey!, { clientId: env.workosClientId! });
  }
  return client;
}

export function authorizationUrl(returnTo = "/queue"): string {
  const url = workos().userManagement.getAuthorizationUrl({
    clientId: env.workosClientId!,
    redirectUri: env.workosRedirectUri,
    provider: "authkit",
    state: Buffer.from(JSON.stringify({ returnTo })).toString("base64url"),
  });
  return url;
}

interface MembershipLike {
  role?: { slug?: string } | string;
  roles?: (string | { slug?: string })[];
}

function roleFromMembership(membership: MembershipLike | null | undefined): Role | null {
  if (!membership) return null;
  const role = membership.role;
  if (typeof role === "string") return normaliseRole(role);
  if (role?.slug) return normaliseRole(role.slug);
  const first = membership.roles?.[0];
  if (typeof first === "string") return normaliseRole(first);
  if (first?.slug) return normaliseRole(first.slug);
  return null;
}

export async function exchangeCode(code: string, returnTo: string): Promise<{ session: Session; returnTo: string }> {
  const result = await workos().userManagement.authenticateWithCode({
    code,
    clientId: env.workosClientId!,
  });

  const user = result.user;
  let organizationId: string | null = (result as { organizationId?: string }).organizationId ?? null;

  // AuthKit only returns an organisation when one was selected or the user
  // belongs to exactly one. Fall back to their membership list so a single-org
  // customer does not have to pick anything.
  if (!organizationId) {
    try {
      const memberships = await workos().userManagement.listOrganizationMemberships({
        userId: user.id,
        limit: 1,
      });
      organizationId = memberships.data?.[0]?.organizationId ?? null;
    } catch {
      organizationId = null;
    }
  }

  let organizationName: string | null = null;
  let role: Role | null = null;

  if (organizationId) {
    try {
      const organization = await workos().organizations.getOrganization(organizationId);
      organizationName = organization.name;
    } catch {
      organizationName = null;
    }
    try {
      const memberships = await workos().userManagement.listOrganizationMemberships({
        userId: user.id,
        organizationId,
        limit: 1,
      });
      role = roleFromMembership(memberships.data?.[0] as MembershipLike | undefined);
    } catch {
      role = null;
    }
  }

  const name =
    [user.firstName, user.lastName].filter(Boolean).join(" ") || user.email || "Signed-in user";

  return {
    session: {
      userId: user.id,
      email: user.email,
      name,
      organizationId,
      organizationName,
      // A user with no organisation, or one we cannot read a role for, gets the
      // least privilege: they can look, not approve.
      role: role ?? "viewer",
      demo: false,
    },
    returnTo,
  };
}
