import { describe, expect, it } from "vitest";

import {
  PLS_BANNER_BACKGROUND_COLOR,
  PLS_BANNER_BORDER_COLOR,
} from "./PlsTransitionBanner";

describe("PlsTransitionBanner colour pipeline", () => {
  it("derives the border colour from the theme warning token via color-mix", () => {
    // Critique #20.6: the previous implementation hand-rolled
    // ``rgba(255, 196, 0, 0.5)`` literals so a future theme rotation
    // could not propagate. The CSS must now reference ``var(--warning)``
    // through ``color-mix`` so the banner picks up the active theme
    // without a source edit.
    expect(PLS_BANNER_BORDER_COLOR).toContain("color-mix");
    expect(PLS_BANNER_BORDER_COLOR).toContain("var(--warning)");
    expect(PLS_BANNER_BORDER_COLOR).toContain("transparent");
    expect(PLS_BANNER_BORDER_COLOR).not.toMatch(/rgba?\(/);
  });

  it("derives the background fill from the theme warning token via color-mix", () => {
    expect(PLS_BANNER_BACKGROUND_COLOR).toContain("color-mix");
    expect(PLS_BANNER_BACKGROUND_COLOR).toContain("var(--warning)");
    expect(PLS_BANNER_BACKGROUND_COLOR).toContain("transparent");
    expect(PLS_BANNER_BACKGROUND_COLOR).not.toMatch(/rgba?\(/);
  });

  it("keeps the border colour substantially more opaque than the fill", () => {
    // The fill is the calm muted surface; the border is the legibility
    // anchor. If both end up the same opacity the banner stops looking
    // like a banner and starts looking like a flat tinted panel \u2014
    // lock the ordering so a future tweak does not collapse them.
    const borderPct = Number(PLS_BANNER_BORDER_COLOR.match(/(\d+)%/)?.[1] ?? "0");
    const fillPct = Number(PLS_BANNER_BACKGROUND_COLOR.match(/(\d+)%/)?.[1] ?? "0");
    expect(borderPct).toBeGreaterThan(fillPct);
    expect(borderPct).toBeGreaterThanOrEqual(40);
    expect(fillPct).toBeLessThanOrEqual(15);
  });
});
