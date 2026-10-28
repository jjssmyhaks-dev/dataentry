"use server";

import { revalidatePath } from "next/cache";
import { enginePost, EngineError } from "@/lib/engine";
import { currentSession } from "@/lib/session";

export interface ActionResult {
  ok: boolean;
  error?: string;
  detail?: unknown;
}

async function guard(run: () => Promise<unknown>): Promise<ActionResult> {
  try {
    const detail = await run();
    revalidatePath("/queue");
    revalidatePath("/");
    revalidatePath("/activity");
    return { ok: true, detail };
  } catch (error) {
    if (error instanceof EngineError) return { ok: false, error: error.detail };
    return { ok: false, error: error instanceof Error ? error.message : "unexpected error" };
  }
}

async function session() {
  const current = await currentSession();
  if (!current) throw new EngineError(401, "Your session has expired. Sign in again.");
  return current;
}

export async function approveItem(input: {
  itemId: string;
  note?: string;
  edits?: Record<string, string>;
  oneOffFields?: string[];
}): Promise<ActionResult> {
  return guard(async () => {
    const who = await session();
    const cleanEdits: Record<string, string> = {};
    for (const [path, value] of Object.entries(input.edits ?? {})) {
      if (value !== undefined && value !== null) cleanEdits[path] = String(value).trim();
    }
    return enginePost(`/v1/review/items/${input.itemId}/approve`, who, {
      note: input.note ?? null,
      edits: cleanEdits,
      one_off_fields: input.oneOffFields ?? [],
    });
  });
}

export async function rejectItem(input: {
  itemId: string;
  reason: string;
  note?: string;
}): Promise<ActionResult> {
  return guard(async () => {
    const who = await session();
    return enginePost(`/v1/review/items/${input.itemId}/reject`, who, {
      reason: input.reason,
      note: input.note ?? null,
    });
  });
}

export async function holdItem(input: { itemId: string; note: string }): Promise<ActionResult> {
  return guard(async () => {
    const who = await session();
    return enginePost(`/v1/review/items/${input.itemId}/hold`, who, { note: input.note });
  });
}

export async function requestDocument(input: {
  itemId: string;
  channel: string;
  templateKey: string;
  message?: string;
}): Promise<ActionResult> {
  return guard(async () => {
    const who = await session();
    return enginePost(`/v1/review/items/${input.itemId}/request-document`, who, {
      channel: input.channel,
      template_key: input.templateKey,
      message: input.message ?? null,
    });
  });
}

export async function reassignItem(input: {
  itemId: string;
  assignee: string;
  note?: string;
}): Promise<ActionResult> {
  return guard(async () => {
    const who = await session();
    return enginePost(`/v1/review/items/${input.itemId}/reassign`, who, {
      assignee: input.assignee,
      note: input.note ?? null,
    });
  });
}

export async function bulkApprove(input: {
  itemIds: string[];
  note?: string;
  groupBy?: string;
}): Promise<ActionResult> {
  return guard(async () => {
    const who = await session();
    return enginePost("/v1/review/bulk-approve", who, {
      item_ids: input.itemIds,
      note: input.note ?? null,
      group_by: input.groupBy ?? null,
    });
  });
}
