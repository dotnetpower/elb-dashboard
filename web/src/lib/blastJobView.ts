/**
 * Pure formatting helpers that translate raw `BlastJobSummary` rows into
 * what the topbar chip (and any future glance UI) wants to render.
 *
 * Single-responsibility: domain → view-model. No React, no I/O, no styling.
 */
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Loader2,
  type LucideIcon,
} from "lucide-react";

import type { BlastJobSummary } from "@/api/endpoints";

export type BlastJobTone = "running" | "ok" | "fail" | "queued";

export interface BlastJobView {
  Icon: LucideIcon;
  label: string;
  tone: BlastJobTone;
  tooltip: string;
}

/**
 * Map a job's status / phase onto the icon + label + tone the UI shows.
 * The status string is matched loosely so backend wording drift (e.g.
 * "Completed" vs "completed" vs "succeeded") doesn't break the UI.
 */
export function describeBlastJob(job: BlastJobSummary): BlastJobView {
  const status = (job.status || job.phase || "").toLowerCase();
  if (status.includes("complet") || status === "succeeded" || status === "done") {
    return {
      Icon: CheckCircle2,
      label: "Completed",
      tone: "ok",
      tooltip: `Completed: ${job.program} · ${shortDb(job.db)}`,
    };
  }
  if (status.includes("fail") || status === "error") {
    return {
      Icon: AlertTriangle,
      label: "Failed",
      tone: "fail",
      tooltip: `Failed: ${job.error ?? job.status}`,
    };
  }
  if (status.includes("queue") || status.includes("pending")) {
    return {
      Icon: Clock,
      label: "Queued",
      tone: "queued",
      tooltip: `Queued: ${job.program} · ${shortDb(job.db)}`,
    };
  }
  return {
    Icon: Loader2,
    label: humanPhase(job.phase) || "Running",
    tone: "running",
    tooltip: `In progress: ${job.program} · ${shortDb(job.db)} (${job.phase || job.status})`,
  };
}

/** Translate raw phase identifiers into researcher-friendly labels. */
export function humanPhase(phase?: string): string {
  if (!phase) return "";
  const p = phase.toLowerCase();
  if (p.includes("provision")) return "Provisioning";
  if (p.includes("download")) return "Downloading DB";
  if (p.includes("split")) return "Splitting";
  if (p.includes("run")) return "Running";
  return phase;
}

/** Last path segment of a DB identifier, e.g. `nt/v5/nt` → `nt`. */
export function shortDb(db: string): string {
  if (!db) return "?";
  const parts = db.split("/").filter(Boolean);
  return parts[parts.length - 1] || db;
}

/** Truncate a job title for chip-sized display. */
export function shortBlastJobTitle(job: BlastJobSummary, max = 36): string {
  const title = job.job_title || `${job.program} · ${shortDb(job.db)}`;
  return title.length > max ? `${title.slice(0, max - 3)}…` : title;
}
