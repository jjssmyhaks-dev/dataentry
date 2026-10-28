"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { enginePost, EngineError } from "@/lib/engine";
import { currentSession } from "@/lib/session";

function back(error: unknown): never {
  const message =
    error instanceof EngineError
      ? error.detail
      : error instanceof Error
        ? error.message
        : "That change did not save.";
  redirect(`/settings?error=${encodeURIComponent(message)}`);
}

function done(message: string): never {
  revalidatePath("/settings");
  revalidatePath("/");
  redirect(`/settings?ok=${encodeURIComponent(message)}`);
}

export async function setPostingMode(formData: FormData): Promise<void> {
  const session = await currentSession();
  if (!session) redirect("/login");
  const mode = String(formData.get("mode") ?? "");
  try {
    await enginePost("/v1/autonomy/posting-mode", session, {
      mode,
      reason: String(formData.get("reason") ?? "") || "changed from the admin screen",
    });
  } catch (error) {
    back(error);
  }
  done(`Posting mode is now ${mode}.`);
}

export async function setAutonomyStage(formData: FormData): Promise<void> {
  const session = await currentSession();
  if (!session) redirect("/login");
  const stage = Number(formData.get("stage") ?? 0);
  try {
    await enginePost("/v1/autonomy/stage", session, {
      stage,
      reason: String(formData.get("reason") ?? "") || "set from the autonomy screen",
    });
  } catch (error) {
    back(error);
  }
  done(`Autonomy stage set to ${stage}.`);
}

export async function setKillSwitch(formData: FormData): Promise<void> {
  const session = await currentSession();
  if (!session) redirect("/login");
  const engaged = String(formData.get("engaged") ?? "false") === "true";
  try {
    await enginePost("/v1/autonomy/kill-switch", session, {
      engaged,
      reason: String(formData.get("reason") ?? "") || "kill switch from the admin screen",
    });
  } catch (error) {
    back(error);
  }
  done(engaged ? "Kill switch engaged: this tenant is in draft mode." : "Kill switch released.");
}

export async function saveLimits(formData: FormData): Promise<void> {
  const session = await currentSession();
  if (!session) redirect("/login");
  const payload: Record<string, number | boolean> = {};
  const threshold = formData.get("confidence_threshold");
  const valueCap = formData.get("autopost_value_cap");
  const dailyCap = formData.get("daily_autopost_cap");
  const errorLimit = formData.get("error_rate_limit");
  if (threshold) payload.confidence_threshold = Number(threshold);
  if (valueCap) payload.autopost_value_cap = Number(valueCap);
  if (dailyCap) payload.daily_autopost_cap = Number(dailyCap);
  if (errorLimit) payload.error_rate_limit = Number(errorLimit);
  payload.auto_demote_on_breach = formData.get("auto_demote_on_breach") === "on";

  try {
    await enginePost("/v1/settings/limits", session, payload);
  } catch (error) {
    back(error);
  }
  done("Limits saved. They apply to the next document the router sees.");
}

export async function setRuleSetting(formData: FormData): Promise<void> {
  const session = await currentSession();
  if (!session) redirect("/login");
  const ruleKey = String(formData.get("rule_key") ?? "");
  try {
    await enginePost("/v1/settings/rules", session, {
      rule_key: ruleKey,
      doc_type: formData.get("doc_type") ? String(formData.get("doc_type")) : null,
      enabled: formData.get("enabled") === "on",
      severity_override: formData.get("severity_override")
        ? String(formData.get("severity_override"))
        : null,
      settings: {},
    });
  } catch (error) {
    back(error);
  }
  done(`Rule ${ruleKey} updated.`);
}
