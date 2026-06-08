import { describe, expect, it } from "vitest";

import { SCALE_NODE_HARD_MAX, clampNodeCount, sliderMaxFor } from "./scaleNodeCount";

describe("sliderMaxFor", () => {
  it("gives at least 16 headroom for a small cluster", () => {
    expect(sliderMaxFor(1)).toBe(16);
    expect(sliderMaxFor(5)).toBe(16);
    expect(sliderMaxFor(8)).toBe(16);
  });

  it("doubles the current size once past 8 nodes", () => {
    expect(sliderMaxFor(10)).toBe(20);
    expect(sliderMaxFor(25)).toBe(50);
  });

  it("caps at the backend hard max", () => {
    expect(sliderMaxFor(60)).toBe(SCALE_NODE_HARD_MAX);
    expect(sliderMaxFor(1000)).toBe(SCALE_NODE_HARD_MAX);
  });

  it("treats non-finite / zero current as 1", () => {
    expect(sliderMaxFor(0)).toBe(16);
    expect(sliderMaxFor(Number.NaN)).toBe(16);
  });
});

describe("clampNodeCount", () => {
  it("clamps into [1, max] and rounds", () => {
    expect(clampNodeCount(5, 20)).toBe(5);
    expect(clampNodeCount(0, 20)).toBe(1);
    expect(clampNodeCount(-4, 20)).toBe(1);
    expect(clampNodeCount(99, 20)).toBe(20);
    expect(clampNodeCount(3.7, 20)).toBe(4);
  });

  it("falls back to 1 for non-finite input", () => {
    expect(clampNodeCount(Number.NaN, 20)).toBe(1);
    expect(clampNodeCount(Number.POSITIVE_INFINITY, 20)).toBe(1);
  });
});
