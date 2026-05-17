export const DEFAULT_AUTO_WARMUP_DBS = ["core_nt"] as const;
export const AUTO_WARMUP_PREFS_EVENT = "elb-auto-warmup-prefs-changed";

const STORAGE_KEY = "elb-auto-warmup-dbs";

export function readAutoWarmupDbs(): Set<string> {
  if (typeof window === "undefined") return new Set(DEFAULT_AUTO_WARMUP_DBS);
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return new Set(DEFAULT_AUTO_WARMUP_DBS);
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return new Set(DEFAULT_AUTO_WARMUP_DBS);
    return new Set(parsed.filter((value): value is string => typeof value === "string"));
  } catch {
    return new Set(DEFAULT_AUTO_WARMUP_DBS);
  }
}

export function writeAutoWarmupDbs(values: Iterable<string>): Set<string> {
  const next = new Set([...values].filter(Boolean).sort());
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([...next]));
    window.dispatchEvent(new CustomEvent(AUTO_WARMUP_PREFS_EVENT, { detail: [...next] }));
  } catch {
    // Private browsing / quota: keep the caller's in-memory state useful.
  }
  return next;
}

export function setAutoWarmupDb(dbName: string, enabled: boolean): Set<string> {
  const next = readAutoWarmupDbs();
  if (enabled) next.add(dbName);
  else next.delete(dbName);
  return writeAutoWarmupDbs(next);
}
