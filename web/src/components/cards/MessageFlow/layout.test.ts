import { describe, expect, it } from "vitest";

import { boxWidth, querySizeLabel } from "./layout";

describe("boxWidth", () => {
  it("returns the minimum width for null/zero query size", () => {
    expect(boxWidth(null)).toBe(56);
    expect(boxWidth(0)).toBe(56);
    expect(boxWidth(undefined)).toBe(56);
  });

  it("grows with query size but stays within bounds", () => {
    const small = boxWidth(400);
    const large = boxWidth(12000);
    expect(large).toBeGreaterThan(small);
    expect(large).toBeLessThanOrEqual(240);
    expect(small).toBeGreaterThanOrEqual(56);
  });

  it("caps very large queries at the maximum width", () => {
    expect(boxWidth(10_000_000_000)).toBe(240);
  });
});

describe("querySizeLabel", () => {
  it("renders a dash for unknown size", () => {
    expect(querySizeLabel(null)).toBe("—");
  });

  it("renders k letters for large sizes", () => {
    expect(querySizeLabel(12000)).toBe("12.0k letters");
  });

  it("renders raw letters for small sizes", () => {
    expect(querySizeLabel(400)).toBe("400 letters");
  });
});
