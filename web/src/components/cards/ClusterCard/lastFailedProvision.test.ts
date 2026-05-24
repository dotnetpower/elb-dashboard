/**
 * Tests for `lastFailedProvision` localStorage helper. Verifies the
 * 24 h freshness window, malformed-entry pruning, and the round-trip
 * shape so a future schema bump can't quietly degrade the dashboard
 * banner.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Tiny Map-backed window.localStorage shim — vitest's default `node`
// environment has no `window`, and pulling in `jsdom` purely for this
// file would slow down the rest of the suite. Pattern mirrors
// `web/src/pages/blastSubmit/useRecentTaxonomy.test.ts`.
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
const fakeWindow = { localStorage: memoryStorage } as unknown as Window &
  typeof globalThis;
(globalThis as { window?: typeof fakeWindow }).window = fakeWindow;

import {
  clearLastFailedProvision,
  loadLastFailedProvision,
  saveLastFailedProvision,
} from "./lastFailedProvision";

describe("lastFailedProvision", () => {
  beforeEach(() => {
    memoryStorage.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("round-trips a fresh entry", () => {
    saveLastFailedProvision({
      raw: "(BadRequest) ErrCode_InsufficientVCPUQuota",
      clusterName: "elb-cluster-01",
      region: "koreacentral",
      resourceGroup: "rg-elb-cluster",
      subscriptionId: "sub-1",
      when: Date.now(),
    });
    const loaded = loadLastFailedProvision();
    expect(loaded).not.toBeNull();
    expect(loaded?.clusterName).toBe("elb-cluster-01");
    expect(loaded?.region).toBe("koreacentral");
  });

  it("returns null and prunes entries older than 24 h", () => {
    saveLastFailedProvision({
      raw: "x",
      clusterName: "old",
      region: "eastus2",
      resourceGroup: "rg",
      subscriptionId: "s",
      when: Date.now() - (25 * 60 * 60 * 1000), // 25 h ago
    });
    expect(loadLastFailedProvision()).toBeNull();
    // Slot was pruned on read.
    expect(memoryStorage.getItem("elb_last_failed_provision_v1")).toBeNull();
  });

  it("returns null for a malformed entry and prunes the slot", () => {
    memoryStorage.setItem(
      "elb_last_failed_provision_v1",
      JSON.stringify({ raw: 42 }), // wrong type
    );
    expect(loadLastFailedProvision()).toBeNull();
    expect(memoryStorage.getItem("elb_last_failed_provision_v1")).toBeNull();
  });

  it("clear removes the slot", () => {
    saveLastFailedProvision({
      raw: "x",
      clusterName: "c",
      region: "r",
      resourceGroup: "rg",
      subscriptionId: "s",
      when: Date.now(),
    });
    expect(loadLastFailedProvision()).not.toBeNull();
    clearLastFailedProvision();
    expect(loadLastFailedProvision()).toBeNull();
  });
});
