import { describe, expect, it } from "vitest";

import { tierTone } from "./helpers";

describe("tierTone", () => {
  // tierTone is the visual contract that lets a researcher recognise
  // each tier in the subscription-wide ClusterCard without expanding
  // the row. Lock the mapping so a future palette refactor cannot
  // silently collapse two tiers into the same surface tone.

  it("returns distinct backgrounds for heavy / gpu / light", () => {
    const heavy = tierTone("heavy");
    const gpu = tierTone("gpu");
    const light = tierTone("light");

    expect(heavy.background).not.toBe(gpu.background);
    expect(gpu.background).not.toBe(light.background);
    expect(heavy.background).not.toBe(light.background);
  });

  it("is case-insensitive and trims whitespace", () => {
    expect(tierTone("Heavy")).toEqual(tierTone("heavy"));
    expect(tierTone(" HEAVY ")).toEqual(tierTone("heavy"));
  });

  it("falls back to the neutral surface tone for unknown / empty / null tiers", () => {
    const general = tierTone("general");
    expect(tierTone(null)).toEqual(general);
    expect(tierTone(undefined)).toEqual(general);
    expect(tierTone("")).toEqual(general);
    expect(tierTone("nonsense")).toEqual(general);
  });
});
