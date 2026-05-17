import { useCallback, useEffect, useState } from "react";

/**
 * One row in the "recently chosen" taxonomy list. Mirrors only the bits we need
 * to render a chip and to repopulate the form when the user taps a chip again.
 *
 * Persistence shape is deliberately small — anything richer (lineage_ex,
 * synonyms, image, …) is re-fetched via `blastApi.getTaxonomyDetail` /
 * `blastApi.getTaxonomyImage` on focus.
 */
export interface RecentTaxonomyEntry {
  taxid: number;
  scientific_name: string;
  common_name?: string | null;
  rank?: string | null;
  is_inclusive: boolean;
  last_used_at: string;
}

const STORAGE_KEY = "elb-recent-taxonomy";
const MAX_ENTRIES = 8;
const SCHEMA_VERSION = 1;

interface StoredPayload {
  v: number;
  items: RecentTaxonomyEntry[];
}

function safeWindow(): Window | null {
  return typeof window === "undefined" ? null : window;
}

function isPositiveTaxid(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 1;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function sanitiseEntry(raw: unknown): RecentTaxonomyEntry | null {
  if (!raw || typeof raw !== "object") return null;
  const row = raw as Record<string, unknown>;
  if (!isPositiveTaxid(row.taxid)) return null;
  if (!isNonEmptyString(row.scientific_name)) return null;
  if (typeof row.is_inclusive !== "boolean") return null;
  const lastUsed = isNonEmptyString(row.last_used_at)
    ? row.last_used_at
    : new Date().toISOString();
  return {
    taxid: row.taxid,
    scientific_name: row.scientific_name.slice(0, 240),
    common_name: isNonEmptyString(row.common_name) ? row.common_name.slice(0, 240) : null,
    rank: isNonEmptyString(row.rank) ? row.rank.slice(0, 60) : null,
    is_inclusive: row.is_inclusive,
    last_used_at: lastUsed,
  };
}

function loadFromStorage(): RecentTaxonomyEntry[] {
  const win = safeWindow();
  if (!win) return [];
  let raw: string | null = null;
  try {
    raw = win.localStorage.getItem(STORAGE_KEY);
  } catch {
    return [];
  }
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!parsed || typeof parsed !== "object") return [];
  const payload = parsed as Partial<StoredPayload>;
  if (payload.v !== SCHEMA_VERSION) return [];
  if (!Array.isArray(payload.items)) return [];
  const out: RecentTaxonomyEntry[] = [];
  const seen = new Set<number>();
  for (const item of payload.items) {
    const clean = sanitiseEntry(item);
    if (!clean || seen.has(clean.taxid)) continue;
    seen.add(clean.taxid);
    out.push(clean);
    if (out.length >= MAX_ENTRIES) break;
  }
  return out;
}

function writeToStorage(items: RecentTaxonomyEntry[]) {
  const win = safeWindow();
  if (!win) return;
  try {
    const payload: StoredPayload = { v: SCHEMA_VERSION, items };
    win.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  } catch {
    /* localStorage may be full or denied; ignore */
  }
}

export interface UseRecentTaxonomy {
  entries: RecentTaxonomyEntry[];
  push: (entry: Omit<RecentTaxonomyEntry, "last_used_at">) => void;
  remove: (taxid: number) => void;
  clear: () => void;
}

/**
 * React hook around `localStorage[elb-recent-taxonomy]`.
 *
 * - Max 8 entries (FIFO drop of oldest on push).
 * - Pushing an existing taxid moves it to the front and refreshes its mode.
 * - All writes are best-effort; storage failures (quota, sandboxed iframe) are
 *   silently ignored so the modal never crashes.
 */
export function useRecentTaxonomy(): UseRecentTaxonomy {
  const [entries, setEntries] = useState<RecentTaxonomyEntry[]>(() => loadFromStorage());

  // Cross-tab sync: refresh on storage events.
  useEffect(() => {
    const win = safeWindow();
    if (!win) return;
    const onStorage = (event: StorageEvent) => {
      if (event.key !== STORAGE_KEY) return;
      setEntries(loadFromStorage());
    };
    win.addEventListener("storage", onStorage);
    return () => win.removeEventListener("storage", onStorage);
  }, []);

  const push = useCallback((entry: Omit<RecentTaxonomyEntry, "last_used_at">) => {
    if (!isPositiveTaxid(entry.taxid)) return;
    if (!isNonEmptyString(entry.scientific_name)) return;
    if (typeof entry.is_inclusive !== "boolean") return;
    setEntries((prev) => {
      const filtered = prev.filter((row) => row.taxid !== entry.taxid);
      const next: RecentTaxonomyEntry[] = [
        {
          taxid: entry.taxid,
          scientific_name: entry.scientific_name.slice(0, 240),
          common_name: entry.common_name?.slice(0, 240) ?? null,
          rank: entry.rank?.slice(0, 60) ?? null,
          is_inclusive: entry.is_inclusive,
          last_used_at: new Date().toISOString(),
        },
        ...filtered,
      ].slice(0, MAX_ENTRIES);
      writeToStorage(next);
      return next;
    });
  }, []);

  const remove = useCallback((taxid: number) => {
    if (!isPositiveTaxid(taxid)) return;
    setEntries((prev) => {
      const next = prev.filter((row) => row.taxid !== taxid);
      writeToStorage(next);
      return next;
    });
  }, []);

  const clear = useCallback(() => {
    writeToStorage([]);
    setEntries([]);
  }, []);

  return { entries, push, remove, clear };
}

export const RECENT_TAXONOMY_MAX_ENTRIES = MAX_ENTRIES;
export const RECENT_TAXONOMY_STORAGE_KEY = STORAGE_KEY;
export const RECENT_TAXONOMY_SCHEMA_VERSION = SCHEMA_VERSION;

// ───────────────────────────────────────────────────────────────────────────
// Test-only pure helpers. Exported so vitest can exercise the storage shape
// without spinning up React. Not part of the public component API.
// ───────────────────────────────────────────────────────────────────────────
export const __test_only__ = {
  loadFromStorage,
  writeToStorage,
  sanitiseEntry,
  pushReducer(
    prev: RecentTaxonomyEntry[],
    entry: Omit<RecentTaxonomyEntry, "last_used_at">,
    nowIso: string,
  ): RecentTaxonomyEntry[] {
    if (!isPositiveTaxid(entry.taxid)) return prev;
    if (!isNonEmptyString(entry.scientific_name)) return prev;
    if (typeof entry.is_inclusive !== "boolean") return prev;
    const filtered = prev.filter((row) => row.taxid !== entry.taxid);
    return [
      {
        taxid: entry.taxid,
        scientific_name: entry.scientific_name.slice(0, 240),
        common_name: entry.common_name?.slice(0, 240) ?? null,
        rank: entry.rank?.slice(0, 60) ?? null,
        is_inclusive: entry.is_inclusive,
        last_used_at: nowIso,
      },
      ...filtered,
    ].slice(0, MAX_ENTRIES);
  },
};
