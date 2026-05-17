import { beforeEach, describe, expect, it } from "vitest";

// Tests for the recent-taxonomy storage helpers. These helpers expect a
// browser-like `window.localStorage`; the default vitest environment is
// `node`, so we provide a tiny Map-backed stand-in here instead of pulling in
// `jsdom` just for this file.
class MemoryStorage {
  private readonly store = new Map<string, string>();
  get length(): number {
    return this.store.size;
  }
  clear(): void {
    this.store.clear();
  }
  getItem(key: string): string | null {
    return this.store.has(key) ? this.store.get(key)! : null;
  }
  setItem(key: string, value: string): void {
    this.store.set(key, value);
  }
  removeItem(key: string): void {
    this.store.delete(key);
  }
  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null;
  }
}

const memoryStorage = new MemoryStorage();
const fakeWindow = {
  localStorage: memoryStorage,
  addEventListener: () => {},
  removeEventListener: () => {},
} as unknown as Window & typeof globalThis;
(globalThis as { window?: typeof fakeWindow }).window = fakeWindow;

import {
  RECENT_TAXONOMY_MAX_ENTRIES,
  RECENT_TAXONOMY_SCHEMA_VERSION,
  RECENT_TAXONOMY_STORAGE_KEY,
  __test_only__,
  type RecentTaxonomyEntry,
} from "@/pages/blastSubmit/useRecentTaxonomy";

const { loadFromStorage, writeToStorage, sanitiseEntry, pushReducer } = __test_only__;

function makeEntry(overrides: Partial<RecentTaxonomyEntry> = {}): RecentTaxonomyEntry {
  return {
    taxid: 9606,
    scientific_name: "Homo sapiens",
    common_name: "human",
    rank: "species",
    is_inclusive: true,
    last_used_at: "2026-05-17T00:00:00.000Z",
    ...overrides,
  };
}

beforeEach(() => {
  memoryStorage.clear();
});

describe("useRecentTaxonomy storage helpers", () => {
  it("returns [] when storage is empty", () => {
    expect(loadFromStorage()).toEqual([]);
  });

  it("ignores payloads with the wrong schema version", () => {
    memoryStorage.setItem(
      RECENT_TAXONOMY_STORAGE_KEY,
      JSON.stringify({ v: 999, items: [makeEntry()] }),
    );
    expect(loadFromStorage()).toEqual([]);
  });

  it("ignores malformed JSON", () => {
    memoryStorage.setItem(RECENT_TAXONOMY_STORAGE_KEY, "not json");
    expect(loadFromStorage()).toEqual([]);
  });

  it("round-trips a clean payload", () => {
    const entries = [makeEntry()];
    writeToStorage(entries);
    expect(loadFromStorage()).toEqual(entries);
  });

  it("drops entries above the cap on load", () => {
    const items = Array.from({ length: RECENT_TAXONOMY_MAX_ENTRIES + 4 }, (_, i) =>
      makeEntry({ taxid: 100 + i, scientific_name: `Org ${i}` }),
    );
    writeToStorage(items);
    expect(loadFromStorage()).toHaveLength(RECENT_TAXONOMY_MAX_ENTRIES);
  });

  it("de-duplicates by taxid on load (keeps the first occurrence)", () => {
    const items = [
      makeEntry({ taxid: 9606, scientific_name: "Homo sapiens" }),
      makeEntry({ taxid: 9606, scientific_name: "Imposter" }),
      makeEntry({ taxid: 562, scientific_name: "E. coli" }),
    ];
    writeToStorage(items);
    const loaded = loadFromStorage();
    expect(loaded.map((row) => row.taxid)).toEqual([9606, 562]);
    expect(loaded[0].scientific_name).toBe("Homo sapiens");
  });
});

describe("sanitiseEntry", () => {
  it("rejects entries without a positive taxid", () => {
    expect(sanitiseEntry({ ...makeEntry(), taxid: 0 })).toBeNull();
    expect(sanitiseEntry({ ...makeEntry(), taxid: -1 })).toBeNull();
    expect(sanitiseEntry({ ...makeEntry(), taxid: "9606" })).toBeNull();
    expect(sanitiseEntry({ ...makeEntry(), taxid: 1.5 })).toBeNull();
    expect(sanitiseEntry(null)).toBeNull();
    expect(sanitiseEntry("not an object")).toBeNull();
  });

  it("rejects entries without a scientific name", () => {
    expect(sanitiseEntry({ ...makeEntry(), scientific_name: "" })).toBeNull();
    expect(sanitiseEntry({ ...makeEntry(), scientific_name: 123 })).toBeNull();
  });

  it("rejects entries with a non-boolean is_inclusive", () => {
    expect(sanitiseEntry({ ...makeEntry(), is_inclusive: "true" })).toBeNull();
  });

  it("clamps long strings", () => {
    const long = "x".repeat(500);
    const cleaned = sanitiseEntry({ ...makeEntry(), scientific_name: long });
    expect(cleaned).not.toBeNull();
    expect(cleaned!.scientific_name.length).toBe(240);
  });

  it("backfills last_used_at when missing or invalid", () => {
    const cleaned = sanitiseEntry({ ...makeEntry(), last_used_at: "" });
    expect(cleaned).not.toBeNull();
    expect(cleaned!.last_used_at.length).toBeGreaterThan(0);
  });
});

describe("pushReducer", () => {
  const NOW = "2026-05-17T12:34:56.000Z";

  it("inserts a new entry at the head", () => {
    const next = pushReducer([], {
      taxid: 9606,
      scientific_name: "Homo sapiens",
      common_name: "human",
      rank: "species",
      is_inclusive: true,
    }, NOW);
    expect(next).toHaveLength(1);
    expect(next[0].taxid).toBe(9606);
    expect(next[0].last_used_at).toBe(NOW);
  });

  it("moves an existing taxid to the head and refreshes mode/time", () => {
    const prev: RecentTaxonomyEntry[] = [
      makeEntry({ taxid: 562, scientific_name: "E. coli" }),
      makeEntry({ taxid: 9606, scientific_name: "Homo sapiens", is_inclusive: false }),
    ];
    const next = pushReducer(prev, {
      taxid: 9606,
      scientific_name: "Homo sapiens",
      common_name: "human",
      rank: "species",
      is_inclusive: true,
    }, NOW);
    expect(next.map((r) => r.taxid)).toEqual([9606, 562]);
    expect(next[0].is_inclusive).toBe(true);
    expect(next[0].last_used_at).toBe(NOW);
  });

  it("caps at MAX_ENTRIES on push", () => {
    const prev: RecentTaxonomyEntry[] = Array.from(
      { length: RECENT_TAXONOMY_MAX_ENTRIES },
      (_, i) => makeEntry({ taxid: 100 + i, scientific_name: `Org ${i}` }),
    );
    const next = pushReducer(prev, {
      taxid: 9606,
      scientific_name: "Homo sapiens",
      is_inclusive: true,
    }, NOW);
    expect(next).toHaveLength(RECENT_TAXONOMY_MAX_ENTRIES);
    expect(next[0].taxid).toBe(9606);
    // Oldest pre-existing entry must have been dropped.
    expect(next.map((r) => r.taxid)).not.toContain(100 + RECENT_TAXONOMY_MAX_ENTRIES - 1);
  });

  it("ignores invalid taxid pushes", () => {
    const prev: RecentTaxonomyEntry[] = [makeEntry()];
    expect(pushReducer(prev, {
      // @ts-expect-error intentional: testing runtime guard
      taxid: "9606",
      scientific_name: "Homo sapiens",
      is_inclusive: true,
    }, NOW)).toBe(prev);
    expect(pushReducer(prev, {
      taxid: 0,
      scientific_name: "Homo sapiens",
      is_inclusive: true,
    }, NOW)).toBe(prev);
    expect(pushReducer(prev, {
      taxid: 9606,
      scientific_name: "",
      is_inclusive: true,
    }, NOW)).toBe(prev);
  });
});

describe("storage payload contract", () => {
  it("uses the documented schema version", () => {
    expect(RECENT_TAXONOMY_SCHEMA_VERSION).toBe(1);
  });

  it("writes payloads under the documented key", () => {
    writeToStorage([makeEntry()]);
    const raw = memoryStorage.getItem(RECENT_TAXONOMY_STORAGE_KEY);
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw!);
    expect(parsed.v).toBe(RECENT_TAXONOMY_SCHEMA_VERSION);
    expect(Array.isArray(parsed.items)).toBe(true);
  });
});
