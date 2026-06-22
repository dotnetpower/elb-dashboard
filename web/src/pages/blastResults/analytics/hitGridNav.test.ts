import { describe, expect, it } from "vitest";

import {
  computeHitGridFocus,
  isHitGridNavKey,
  type HitGridKey,
} from "./hitGridNav";

describe("computeHitGridFocus", () => {
  it("returns no focus for an empty hit set", () => {
    expect(
      computeHitGridFocus({ key: "ArrowDown", focusedRow: -1, paintedCount: 0, totalCount: 0 }),
    ).toEqual({ nextRow: -1, loadMore: false });
  });

  it("ArrowDown from no focus lands on row 0 without loading", () => {
    expect(
      computeHitGridFocus({ key: "ArrowDown", focusedRow: -1, paintedCount: 60, totalCount: 500 }),
    ).toEqual({ nextRow: 0, loadMore: false });
  });

  it("ArrowDown within painted rows advances without loading", () => {
    expect(
      computeHitGridFocus({ key: "ArrowDown", focusedRow: 5, paintedCount: 60, totalCount: 500 }),
    ).toEqual({ nextRow: 6, loadMore: false });
  });

  it("ArrowDown on the last painted row loads more and advances into it", () => {
    // 60 painted (indices 0..59); focus on 59 → 60 needs the next window batch.
    expect(
      computeHitGridFocus({ key: "ArrowDown", focusedRow: 59, paintedCount: 60, totalCount: 500 }),
    ).toEqual({ nextRow: 60, loadMore: true });
  });

  it("ArrowDown on the very last row of the set stays put (no wrap, no load)", () => {
    expect(
      computeHitGridFocus({ key: "ArrowDown", focusedRow: 499, paintedCount: 500, totalCount: 500 }),
    ).toEqual({ nextRow: 499, loadMore: false });
  });

  it("ArrowUp never loads and never goes below 0", () => {
    expect(
      computeHitGridFocus({ key: "ArrowUp", focusedRow: 6, paintedCount: 60, totalCount: 500 }),
    ).toEqual({ nextRow: 5, loadMore: false });
    expect(
      computeHitGridFocus({ key: "ArrowUp", focusedRow: 0, paintedCount: 60, totalCount: 500 }),
    ).toEqual({ nextRow: 0, loadMore: false });
  });

  it("Home focuses row 0; End focuses the last painted row (not the absolute last)", () => {
    expect(
      computeHitGridFocus({ key: "Home", focusedRow: 40, paintedCount: 60, totalCount: 500 }),
    ).toEqual({ nextRow: 0, loadMore: false });
    expect(
      computeHitGridFocus({ key: "End", focusedRow: 5, paintedCount: 60, totalCount: 500 }),
    ).toEqual({ nextRow: 59, loadMore: false });
  });

  it("End on a fully-painted set focuses the true last row", () => {
    expect(
      computeHitGridFocus({ key: "End", focusedRow: 0, paintedCount: 500, totalCount: 500 }),
    ).toEqual({ nextRow: 499, loadMore: false });
  });

  it("clamps an out-of-range starting index into the valid window", () => {
    expect(
      computeHitGridFocus({ key: "ArrowUp", focusedRow: 9999, paintedCount: 60, totalCount: 500 }),
    ).toEqual({ nextRow: 498, loadMore: false });
  });

  it("a full walk to the end never skips or repeats a row across load seams", () => {
    const total = 130;
    const step = 60;
    let painted = 60;
    let focused = 0;
    const visited: number[] = [focused];
    for (let i = 0; i < total - 1; i += 1) {
      const result = computeHitGridFocus({
        key: "ArrowDown",
        focusedRow: focused,
        paintedCount: painted,
        totalCount: total,
      });
      if (result.loadMore) painted = Math.min(painted + step, total);
      expect(result.nextRow).toBe(focused + 1);
      focused = result.nextRow;
      visited.push(focused);
    }
    expect(focused).toBe(total - 1);
    expect(new Set(visited).size).toBe(total);
  });
});

describe("isHitGridNavKey", () => {
  it("recognises the four navigation keys and rejects others", () => {
    for (const key of ["ArrowDown", "ArrowUp", "Home", "End"] as HitGridKey[]) {
      expect(isHitGridNavKey(key)).toBe(true);
    }
    expect(isHitGridNavKey("Enter")).toBe(false);
    expect(isHitGridNavKey("Tab")).toBe(false);
    expect(isHitGridNavKey("a")).toBe(false);
  });
});
