import { notFound } from "next/navigation";
import { ReviewWorkspace } from "@/components/review-workspace";
import { PageHeader, Problem } from "@/components/ui";
import { engineGet, EngineError, type QueueResponse, type ReviewItemDetail } from "@/lib/engine";
import { atLeast, currentSession } from "@/lib/session";

export const dynamic = "force-dynamic";

const CONFIDENCE_THRESHOLD = 0.93;
const APPROVAL_LIMIT = 250_000;

export default async function ReviewItemPage({ params }: { params: Promise<{ id: string }> }) {
  const session = await currentSession();
  if (!session) return null;
  const { id } = await params;

  let detail: ReviewItemDetail | null = null;
  let failure: EngineError | null = null;
  let nextId: string | null = null;

  try {
    detail = await engineGet<ReviewItemDetail>(`/v1/review/items/${id}`, session);
  } catch (error) {
    if (error instanceof EngineError) {
      if (error.status === 404) notFound();
      failure = error;
    } else throw error;
  }

  if (failure || !detail) {
    return (
      <div className="space-y-6">
        <PageHeader eyebrow="Review" title="Record" />
        <Problem
          title={failure?.isOffline ? "The engine is not running" : "Could not load this record"}
          detail={failure?.detail ?? "Unknown error."}
        />
      </div>
    );
  }

  try {
    const queue = await engineGet<QueueResponse>("/v1/review/queue", session, {
      status: "pending,held,clarification_requested",
      limit: 20,
    });
    nextId = queue.items.find((row) => row.id !== detail!.item.id)?.id ?? null;
  } catch {
    nextId = null;
  }

  return (
    <ReviewWorkspace
      detail={detail}
      nextId={nextId}
      canAct={atLeast(session.role, "reviewer")}
      approvalLimit={APPROVAL_LIMIT}
      confidenceThreshold={CONFIDENCE_THRESHOLD}
    />
  );
}
