import { configFromTags } from "./configFromTags";

export interface DiscoveryRg {
  name: string;
  location: string;
  tags?: Record<string, string>;
}

/**
 * True when at least one resource group in the list is a recognisable
 * ElasticBLAST workspace (carries `elb-*` tags). Mirrors the predicate the
 * discovery effect uses so the hook can decide whether the *direct* ARM list
 * already covers the workspace before paying for a second MI-proxy round trip.
 */
export function hasElbWorkspace(
  subscriptionId: string,
  rgs: DiscoveryRg[],
): boolean {
  return rgs.some((rg) => configFromTags(subscriptionId, rg) != null);
}

/**
 * Merge two resource-group lists by name. Entries from `secondary` add to or
 * override matching `primary` entries. The backend MI proxy is the authoritative
 * subscription-wide source (it lists every RG with tags via the shared
 * identity's Reader role), so its tags win when both sources name the same RG.
 */
export function mergeRgsByName(
  primary: DiscoveryRg[],
  secondary: DiscoveryRg[],
): DiscoveryRg[] {
  const byName = new Map<string, DiscoveryRg>();
  for (const rg of primary) byName.set(rg.name, rg);
  for (const rg of secondary) byName.set(rg.name, rg);
  return Array.from(byName.values());
}
