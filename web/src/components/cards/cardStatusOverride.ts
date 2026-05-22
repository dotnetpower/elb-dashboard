/**
 * Bridge between `getDegradedInfo` and `MonitorCard.statusOverride`.
 *
 * Cards typically need two things from a degraded payload:
 *   - a chip label + tone for the header (so "OK" is not shown when ARM
 *     actually returned 401), and
 *   - a banner-friendly description for the card body.
 *
 * This helper centralises the tone mapping so every card surfaces the same
 * colour for the same reason class.
 */

import type { DegradedInfo } from "@/utils/monitorDegraded";

export interface CardStatusOverride {
  label: string;
  tone: "warning" | "danger" | "muted";
  title?: string;
}

/**
 * Returns a `statusOverride` for `MonitorCard` when the payload is degraded,
 * otherwise null. The card may still pass its own `status` prop; the override
 * wins when present.
 */
export function degradedStatusOverride(
  info: DegradedInfo,
): CardStatusOverride | null {
  if (!info.degraded || !info.reason) {
    return null;
  }
  return {
    label: info.label,
    tone: toneForReason(info.reason),
    title: info.description,
  };
}

function toneForReason(
  reason: string,
): "warning" | "danger" | "muted" {
  switch (reason) {
    case "auth_wrong_tenant":
    case "unauthorized":
    case "access_denied":
      return "danger";
    case "forbidden":
    case "network_blocked":
    case "firewall_blocked":
      return "warning";
    case "not_found":
      return "muted";
    default:
      return "warning";
  }
}
