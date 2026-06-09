import type { VnetPeeringExistingItem } from "@/api/settings";

export type PeeringHealth = "healthy" | "ghost" | "disconnected";

/**
 * Classify an existing peering for the Settings UI.
 *
 * - `ghost`        — the remote VNet was confirmed deleted
 *                    (`remote_vnet_exists === false`). Definitely stale; the
 *                    operator should delete the peering.
 * - `disconnected` — the peering is in the `Disconnected` state but the remote
 *                    VNet could not be confirmed gone (`remote_vnet_exists`
 *                    null — RBAC / cross-tenant). Probably stale; surface a
 *                    softer warning.
 * - `healthy`      — Connected / Initiated, or any state with a live remote VNet.
 */
export function classifyPeering(item: VnetPeeringExistingItem): PeeringHealth {
  if (item.remote_vnet_exists === false) return "ghost";
  const disconnected = item.peering_state.toLowerCase().includes("disconnect");
  if (disconnected && item.remote_vnet_exists !== true) return "disconnected";
  return "healthy";
}

/** True when the peering is stale enough to offer delete/hide affordances. */
export function isStalePeering(item: VnetPeeringExistingItem): boolean {
  return classifyPeering(item) !== "healthy";
}
