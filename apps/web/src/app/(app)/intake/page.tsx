import { IntakeForm } from "@/components/intake-form";
import { PageHeader } from "@/components/ui";
import { engineGet, EngineError, type Dashboard } from "@/lib/engine";
import { currentSession } from "@/lib/session";
import { number } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function IntakePage() {
  const session = await currentSession();
  if (!session) return null;

  let recent: Dashboard["documents"] | null = null;
  try {
    const dashboard = await engineGet<Dashboard>("/v1/dashboard", session, { days: 1 });
    recent = dashboard.documents;
  } catch (error) {
    if (!(error instanceof EngineError)) throw error;
  }

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Intake"
        title="Add a document"
        description={
          recent
            ? `${number(recent.documents)} documents arrived in the last day; ${number(
                recent.posted,
              )} posted and ${number(recent.in_review)} are waiting for a person. Web upload is one of five channels — email, WhatsApp, bulk upload and the product API feed the same pipeline.`
            : "Attach a document or paste its text. The classifier, extractor and rule engine run immediately."
        }
      />
      <IntakeForm />
    </div>
  );
}
