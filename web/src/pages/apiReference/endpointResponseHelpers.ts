/**
 * endpointResponseHelpers — pure response-formatting helpers for EndpointCard.
 *
 * Extracted from `EndpointCard.tsx` (issue #24) so the ~900-line card component
 * keeps the render/try-it logic and these stateless formatters live in a small,
 * unit-testable module. No React, no state, no fetches.
 */
import type { SpecEndpoint } from "@/pages/apiReference/types";

export type ResponseEntry = [string, NonNullable<SpecEndpoint["responses"]>[string]];

/** Parse a JSON string, returning `null` on empty input or a parse error. */
export function safeParseJson(text: string): unknown {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** Sort response entries numerically by status code, non-numeric codes last. */
export function sortResponses(entries: ResponseEntry[]): ResponseEntry[] {
  return [...entries].sort(([left], [right]) => {
    const leftNumber = Number(left);
    const rightNumber = Number(right);
    if (Number.isFinite(leftNumber) && Number.isFinite(rightNumber)) {
      return leftNumber - rightNumber;
    }
    if (Number.isFinite(leftNumber)) return -1;
    if (Number.isFinite(rightNumber)) return 1;
    return left.localeCompare(right);
  });
}

/** Hint shown when a path uses the `{job_id}` template (OpenAPI id, not UUID). */
export function getPathIdHint(
  path: string,
): { label: string; title: string } | undefined {
  if (!path.includes("{job_id}")) return undefined;
  return {
    label: "job_id = OpenAPI id",
    title:
      "Use the short OpenAPI job id returned by POST /v1/jobs, not the Dashboard UUID.",
  };
}

/** Human-readable response title from a status code + optional description. */
export function responseTitle(code: string, description?: string): string {
  if (description && description !== "Successful Response") return description;
  if (code.startsWith("2")) return "SuccessResponse";
  if (code.startsWith("4")) return "ErrorResponse";
  if (code.startsWith("5")) return "RuntimeFailure";
  return `HTTP${code}`;
}

/** Foreground colour token for a response status code. */
export function responseTone(code: string): string {
  if (code.startsWith("2")) return "var(--success)";
  if (code === "409" || code === "429") return "var(--warning)";
  if (code.startsWith("4") || code.startsWith("5")) return "var(--danger)";
  return "var(--text-faint)";
}

/** Background colour token for a response status code. */
export function responseBackground(code: string): string {
  if (code.startsWith("2")) return "rgba(115,191,105,0.08)";
  if (code === "409" || code === "429") return "rgba(245,166,35,0.08)";
  if (code.startsWith("4") || code.startsWith("5")) return "rgba(242,114,111,0.08)";
  return "var(--bg-tertiary)";
}

/** Border colour token for a response status code. */
export function responseBorder(code: string): string {
  if (code.startsWith("2")) return "rgba(115,191,105,0.16)";
  if (code === "409" || code === "429") return "rgba(245,166,35,0.18)";
  if (code.startsWith("4") || code.startsWith("5")) return "rgba(242,114,111,0.18)";
  return "var(--border-weak)";
}
