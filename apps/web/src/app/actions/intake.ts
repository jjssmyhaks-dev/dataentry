"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import {
  enginePost,
  EngineError,
  uploadDocument,
  type IngestResult,
} from "@/lib/engine";
import { currentSession } from "@/lib/session";

export interface IntakeActionResult {
  ok: boolean;
  error?: string;
  result?: IngestResult;
}

async function sessionOrThrow() {
  const session = await currentSession();
  if (!session) throw new EngineError(401, "Your session has expired. Sign in again.");
  return session;
}

export async function uploadIntake(formData: FormData): Promise<IntakeActionResult> {
  try {
    const session = await sessionOrThrow();
    const file = formData.get("file");
    const text = String(formData.get("text") ?? "").trim();
    const channel = String(formData.get("channel") ?? "web_upload");

    let result: IngestResult;
    if (file instanceof File && file.size > 0) {
      result = await uploadDocument(session, file, channel);
    } else if (text) {
      result = await enginePost<IngestResult>("/v1/documents", session, {
        text,
        channel: "web_upload",
        file_name: "pasted-document.txt",
        mime_type: "text/plain",
      });
    } else {
      return { ok: false, error: "Attach a file or paste the document text." };
    }
    revalidatePath("/queue");
    revalidatePath("/");
    return { ok: true, result };
  } catch (error) {
    return {
      ok: false,
      error:
        error instanceof EngineError
          ? error.detail
          : error instanceof Error
            ? error.message
            : "Upload failed.",
    };
  }
}

export async function retryException(formData: FormData): Promise<void> {
  const session = await sessionOrThrow();
  const documentId = String(formData.get("document_id") ?? "");
  try {
    await enginePost(`/v1/exceptions/${documentId}/retry`, session);
  } catch (error) {
    const detail = error instanceof EngineError ? error.detail : "Retry failed.";
    redirect(`/exceptions?error=${encodeURIComponent(detail)}`);
  }
  revalidatePath("/exceptions");
  revalidatePath("/queue");
  redirect("/exceptions?ok=Re-read+queued+for+this+document.");
}

export async function rejectException(formData: FormData): Promise<void> {
  const session = await sessionOrThrow();
  const documentId = String(formData.get("document_id") ?? "");
  const reason = String(formData.get("reason") ?? "").trim() || "not_a_business_document";
  try {
    await enginePost(
      `/v1/exceptions/${documentId}/reject?reason=${encodeURIComponent(reason)}`,
      session,
    );
  } catch (error) {
    const detail = error instanceof EngineError ? error.detail : "Reject failed.";
    redirect(`/exceptions?error=${encodeURIComponent(detail)}`);
  }
  revalidatePath("/exceptions");
  redirect("/exceptions?ok=Exception+closed.");
}
