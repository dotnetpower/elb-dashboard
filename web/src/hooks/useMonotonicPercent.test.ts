import { describe, expect, it } from "vitest";

import { nextMonotonicFloor } from "./useMonotonicPercent";

describe("nextMonotonicFloor", () => {
  it("rises when the raw value increases", () => {
    expect(nextMonotonicFloor(20, 35, true)).toBe(35);
  });

  it("holds the floor when the raw value dips within a session", () => {
    // The warmup saw-tooth: a shard finishing its copy drops to 0 in the raw
    // signal; the displayed floor must stay at the previous high.
    expect(nextMonotonicFloor(80, 0, true)).toBe(80);
  });

  it("holds the floor when the raw field is missing (null/undefined)", () => {
    // A transient blob-listing failure drops the `success` field entirely.
    expect(nextMonotonicFloor(60, null, true)).toBe(60);
    expect(nextMonotonicFloor(60, undefined, true)).toBe(60);
  });

  it("resets to the raw value when the session changes", () => {
    // A new run (different resetKey, or active flips false) must start over
    // rather than being pinned at the previous run's 100%.
    expect(nextMonotonicFloor(100, 5, false)).toBe(5);
    expect(nextMonotonicFloor(100, 0, false)).toBe(0);
  });

  it("clamps the raw value into the 0..100 range", () => {
    expect(nextMonotonicFloor(0, 150, true)).toBe(100);
    expect(nextMonotonicFloor(0, -10, true)).toBe(0);
  });

  it("ignores NaN raw values without lowering the floor", () => {
    expect(nextMonotonicFloor(42, Number.NaN, true)).toBe(42);
  });
});
