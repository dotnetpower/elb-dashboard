/**
 * Tests for the useStickToBottom user-control decision.
 *
 * Responsibility: Lock the "keep following the tail vs. pause because the
 * user scrolled up" contract so a refactor of the smooth-follow ResizeObserver
 * path cannot silently break the manual-scroll-away behaviour. Also locks the
 * anchor-follow geometry (target scrollTop + follow/pause decision) used when
 * tailing the active step row instead of the document bottom.
 * Edit boundaries: Test only the pure helpers here; the ResizeObserver /
 * window-scroll wiring needs a real layout engine and is covered by manual /
 * e2e validation.
 * Key entry points: `test shouldFollow`, `test shouldFollowAnchor`,
 * `test anchorFollowTarget`.
 * Risky contracts: Threshold default (96 px) must match the hook; the anchor
 * margin (24 px) must match `ANCHOR_BOTTOM_MARGIN_PX`.
 * Validation: `cd web && npm test -- useStickToBottom`.
 */
import { describe, expect, it } from "vitest";

import {
  anchorFollowTarget,
  shouldFollow,
  shouldFollowAnchor,
} from "./useStickToBottom";

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

describe("shouldFollowAnchor", () => {
  it("follows when the viewport bottom is exactly at the anchor bottom", () => {
    // scrollTop(700) + viewport(300) === anchorBottom(1000)
    expect(shouldFollowAnchor(700, 300, 1000)).toBe(true);
  });

  it("follows when within the default 96px threshold above the anchor bottom", () => {
    // anchor bottom 80px below the viewport bottom → still within 96px.
    expect(shouldFollowAnchor(620, 300, 1000)).toBe(true);
  });

  it("pauses when the user scrolled up further than the threshold above the anchor", () => {
    // anchor bottom 200px below the viewport bottom → user reading history.
    expect(shouldFollowAnchor(500, 300, 1000)).toBe(false);
  });

  it("keeps following when scrolled past the anchor bottom (pending rows below)", () => {
    // viewport bottom already below the anchor bottom → still anchored.
    expect(shouldFollowAnchor(900, 300, 1000)).toBe(true);
  });
});

describe("anchorFollowTarget", () => {
  it("aligns the anchor bottom to 24px above the viewport bottom", () => {
    // target = anchorBottom(1000) - viewport(300) + margin(24) = 724
    expect(anchorFollowTarget(1000, 300, 5000)).toBe(724);
  });

  it("clamps to the scrollable maximum so a tall anchor never overscrolls", () => {
    // maxScroll = documentHeight(1000) - viewport(300) = 700; raw target 724.
    expect(anchorFollowTarget(1000, 300, 1000)).toBe(700);
  });

  it("clamps to zero when the anchor sits within the first viewport", () => {
    // raw target = 100 - 300 + 24 = -176 → clamp to 0.
    expect(anchorFollowTarget(100, 300, 5000)).toBe(0);
  });

  it("respects a custom margin", () => {
    expect(anchorFollowTarget(1000, 300, 5000, 0)).toBe(700);
  });
});
