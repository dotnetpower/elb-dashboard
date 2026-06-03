/**
 * Tests for the useStickToBottom user-control decision.
 *
 * Responsibility: Lock the "keep following the tail vs. pause because the
 * user scrolled up" contract so a refactor of the smooth-follow ResizeObserver
 * path cannot silently break the manual-scroll-away behaviour.
 * Edit boundaries: Test only the pure `shouldFollow` helper here; the
 * ResizeObserver / window-scroll wiring needs a real layout engine and is
 * covered by manual / e2e validation.
 * Key entry points: `test shouldFollow`.
 * Risky contracts: Threshold default (96 px) must match the hook.
 * Validation: `cd web && npm test -- useStickToBottom`.
 */
import { describe, expect, it } from "vitest";

import { shouldFollow } from "./useStickToBottom";

describe("shouldFollow", () => {
  it("follows when the viewport is exactly at the bottom", () => {
    // scrollTop(900) + viewport(100) === documentHeight(1000)
    expect(shouldFollow(900, 100, 1000)).toBe(true);
  });

  it("follows when within the default 96px threshold of the bottom", () => {
    // 80px short of the bottom → still within 96px → keep following.
    expect(shouldFollow(820, 100, 1000)).toBe(true);
  });

  it("pauses when the user has scrolled further than the threshold", () => {
    // 200px short of the bottom → user is reading history → pause.
    expect(shouldFollow(700, 100, 1000)).toBe(false);
  });

  it("respects a custom threshold", () => {
    // 150px short of the bottom with a 200px threshold → still following.
    expect(shouldFollow(750, 100, 1000, 200)).toBe(true);
    // Same geometry with the default 96px threshold → paused.
    expect(shouldFollow(750, 100, 1000)).toBe(false);
  });

  it("follows when content is shorter than the viewport (nothing to scroll)", () => {
    expect(shouldFollow(0, 800, 500)).toBe(true);
  });
});
