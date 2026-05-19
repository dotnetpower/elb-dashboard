import { describe, expect, it } from "vitest";

import { parseFeatureFlag } from "./runtime";

describe("parseFeatureFlag", () => {
  it("defaults missing and empty values to the fallback", () => {
    expect(parseFeatureFlag(undefined)).toBe(true);
    expect(parseFeatureFlag("", false)).toBe(false);
    expect(parseFeatureFlag("   ", true)).toBe(true);
  });

  it("recognizes common disabled values", () => {
    for (const value of ["false", "FALSE", "0", "no", "off", "disabled"]) {
      expect(parseFeatureFlag(value)).toBe(false);
    }
  });

  it("recognizes common enabled values", () => {
    for (const value of ["true", "TRUE", "1", "yes", "on", "enabled"]) {
      expect(parseFeatureFlag(value, false)).toBe(true);
    }
  });
});