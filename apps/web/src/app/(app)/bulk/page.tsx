import { BulkApprovePanel } from "@/components/bulk-approve";
import { PageHeader, Problem } from "@/components/ui";
import { engineGet, EngineError, type QueueResponse } from "@/lib/engine";
import { atLeast, currentSession } from "@/lib/session";
import { money } from "@/lib/format";

export const dynamic = "force-dynamic";

const APPROVAL_LIMIT = 250_000;

export default async function BulkPage() {
  const session = await currentSession();
  if (!session) return null;

  let data: QueueResponse | null = null;
  let failure: EngineError | null = null;
  try {
    data = await engineGet<QueueResponse>("/v1/review/queue", session, {
      status: "pending",
      limit: 100,
    });
  } catch (error) {
    if (error instanceof EngineError) failure = error;
    else throw error;
  }

  if (failure || !data) {
    return (
      <div className="space-y-6">
        <PageHeader eyebrow="Review" title="Bulk approve" />
        <Problem
          title={failure?.isOffline ? "The engine is not running" : "Could not load the queue"}
          detail={failure?.detail ?? "No response from the engine."}
        />
      </div>
    );
  }

  // Appendix D: bulk approve only clears the low-risk pile. A rule failure is
  // never swept up in a batch, so it is filtered out here rather than in a query
  // the browser could tamper with.
  const safe = data.items.filter(
    (item) => item.priority_band !== "rule_failure" && !item.requires_supervisor,
  );
  const held = safe.reduce((sum, item) => sum + Number(item.amount_total ?? 0), 0);
  const warnings = safe.filter((item) => (item.warned_rules ?? []).length > 0).length;

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Review"
        title="Bulk approve"
        description={
          <>
            {safe.length} records passed every rule and sit under the single-reviewer limit
            {held > 0 ? <> — {money(held, { compact: true })} in total</> : null}. Group them by
            vendor to clear a whole supplier in one confirmation
            {warnings > 0 ? <>; {warnings} carry a warning that lowers confidence</> : null}.
          </>
        }
      />
      <BulkApprovePanel
        items={safe}
        canAct={atLeast(session.role, "reviewer")}
        approvalLimit={APPROVAL_LIMIT}
      />
    </div>
  );
}
