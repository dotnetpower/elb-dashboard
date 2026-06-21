/**
 * jobSubmissionSource — single source of truth for how a BLAST job was
 * submitted (dashboard UI / external OpenAPI / Service Bus queue).
 *
 * Both the JobRow "User" column and the Recent searches source filter read
 * this so the displayed source and the filter predicate can never disagree.
 * The value lives on `payload.submission_source` (server-derived), with a
 * legacy fallback to `owner_upn === "api"` for rows written before
 * submission_source was captured.
 */
import type { BlastJobSummary } from "@/api/endpoints";

export type JobSource = "ui" | "api" | "servicebus";

/** Resolve the submission source of a job for display + filtering. */
export function jobSubmissionSource(job: BlastJobSummary): JobSource {
  // Prefer the durable top-level field (populated for column-only list rows
  // where `payload` is omitted), then the legacy `payload.submission_source`.
  const raw =
    (typeof job.submission_source === "string" && job.submission_source) ||
    (typeof job.payload?.submission_source === "string"
      ? (job.payload.submission_source as string)
      : null);
  if (raw === "servicebus") return "servicebus";
  // Legacy rows pre-dating owner_upn capture carry the source on the payload;
  // `owner_upn === "api"` is the older external-submit marker.
  if (raw === "external_api" || job.owner_upn === "api") return "api";
  return "ui";
}

/** Short human label for the source (used in the User column). */
export function jobSourceLabel(source: JobSource): string {
  if (source === "servicebus") return "queue";
  if (source === "api") return "api";
  return "ui";
}
