/**
 * Local "hide this ghost peering" preference store.
 *
 * When a VNet peering's remote VNet has been deleted, the peering lingers in
 * the `Disconnected` state. The operator may not have permission (or desire) to
 * delete it from Azure, but still wants it out of the Settings list. This
 * module persists a per-cluster set of dismissed peering names in
 * `localStorage` so the row stays hidden across reloads. It is purely cosmetic
 * — it never touches Azure — and is keyed by cluster so the same peering name
 * on a different cluster is unaffected.
 */

const STORAGE_KEY = "elb-vnet-peering-dismissed";

function compositeKey(clusterName: string, peeringName: string): string {
  return `${clusterName}::${peeringName}`;
}

function readRaw(): Set<string> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return new Set(parsed.filter((x) => typeof x === "string"));
    return new Set();
  } catch {
    return new Set();
  }
}

function writeRaw(keys: Set<string>): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(Array.from(keys)));
  } catch {
    // Best-effort; a full / disabled storage just means no persistence.
  }
}

/** Returns the set of dismissed peering names for a cluster. */
export function readDismissedPeerings(clusterName: string): Set<string> {
  if (!clusterName) return new Set();
  const prefix = `${clusterName}::`;
  const out = new Set<string>();
  for (const key of readRaw()) {
    if (key.startsWith(prefix)) out.add(key.slice(prefix.length));
  }
  return out;
}

/** Marks a peering as dismissed (hidden) for a cluster. Returns the new set. */
export function dismissPeering(clusterName: string, peeringName: string): Set<string> {
  const all = readRaw();
  all.add(compositeKey(clusterName, peeringName));
  writeRaw(all);
  return readDismissedPeerings(clusterName);
}

/** Clears a dismissal (un-hides a peering) for a cluster. Returns the new set. */
export function undismissPeering(clusterName: string, peeringName: string): Set<string> {
  const all = readRaw();
  all.delete(compositeKey(clusterName, peeringName));
  writeRaw(all);
  return readDismissedPeerings(clusterName);
}
