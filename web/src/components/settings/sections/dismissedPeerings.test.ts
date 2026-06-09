/**
 * Tests for the dismissed-peering localStorage store — verifies hide/un-hide is
 * per-cluster and survives a re-read (the "make the ghost disappear" affordance).
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  dismissPeering,
  readDismissedPeerings,
  undismissPeering,
} from "./dismissedPeerings";

// The default vitest environment is node (no DOM), so install a minimal
// in-memory localStorage shim rather than pulling in jsdom as a dependency.
class MemoryStorage {
  private store = new Map<string, string>();
  getItem(key: string): string | null {
    return this.store.has(key) ? (this.store.get(key) as string) : null;
  }
  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }
  removeItem(key: string): void {
    this.store.delete(key);
  }
  clear(): void {
    this.store.clear();
  }
}

beforeEach(() => {
  (globalThis as { localStorage: Storage }).localStorage =
    new MemoryStorage() as unknown as Storage;
});

afterEach(() => {
  delete (globalThis as { localStorage?: Storage }).localStorage;
});

describe("dismissedPeerings", () => {
  it("starts empty", () => {
    expect(readDismissedPeerings("aks-1").size).toBe(0);
  });

  it("hides and reads back a peering per cluster", () => {
    dismissPeering("aks-1", "peer-ghost");
    expect(readDismissedPeerings("aks-1").has("peer-ghost")).toBe(true);
    // A different cluster with the same peering name is unaffected.
    expect(readDismissedPeerings("aks-2").has("peer-ghost")).toBe(false);
  });

  it("un-hides a peering", () => {
    dismissPeering("aks-1", "peer-ghost");
    undismissPeering("aks-1", "peer-ghost");
    expect(readDismissedPeerings("aks-1").has("peer-ghost")).toBe(false);
  });

  it("returns an empty set for a blank cluster name", () => {
    expect(readDismissedPeerings("").size).toBe(0);
  });
});
