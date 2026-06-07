import { describe, expect, it } from "vitest";

import {
  computeRemainingSeconds,
  formatCoarseRemaining,
  formatCountdown,
  stabilizeDeadline,
} from "./autoStopCountdown";

describe("computeRemainingSeconds", () => {
  const anchor = 1_000_000;

  it("returns the full baseline when no client time has elapsed", () => {
    expect(computeRemainingSeconds(600, anchor, anchor)).toBe(600);
  });

  it("subtracts elapsed client time from the baseline", () => {
    // 90s of client time elapsed since the snapshot arrived.
    expect(computeRemainingSeconds(600, anchor, anchor + 90_000)).toBe(510);
  });

  it("is immune to client/server clock skew (anchor + now share one clock)", () => {
    // Even if the absolute wall clock is wildly off, only the elapsed
    // delta between anchor and now matters.
    const skewedAnchor = 5_000_000_000;
    expect(
      computeRemainingSeconds(300, skewedAnchor, skewedAnchor + 60_000),
    ).toBe(240);
  });

  it("clamps to zero once the deadline has passed", () => {
    expect(computeRemainingSeconds(120, anchor, anchor + 200_000)).toBe(0);
  });

  it("clamps to the baseline when the client clock jumps backward", () => {
    // now < anchor would otherwise inflate remaining above the snapshot.
    expect(computeRemainingSeconds(300, anchor, anchor - 120_000)).toBe(300);
  });

  it("treats a non-positive or non-finite baseline as zero", () => {
    expect(computeRemainingSeconds(0, anchor, anchor)).toBe(0);
    expect(computeRemainingSeconds(-5, anchor, anchor)).toBe(0);
    expect(computeRemainingSeconds(Number.NaN, anchor, anchor)).toBe(0);
  });

  it("rounds to the nearest whole second", () => {
    expect(computeRemainingSeconds(600, anchor, anchor + 1_400)).toBe(599);
    expect(computeRemainingSeconds(600, anchor, anchor + 1_600)).toBe(598);
  });
});

describe("formatCountdown", () => {
  it("shows seconds only under a minute", () => {
    expect(formatCountdown(0)).toBe("0s");
    expect(formatCountdown(5)).toBe("5s");
    expect(formatCountdown(59)).toBe("59s");
  });

  it("shows minutes with zero-padded seconds under an hour", () => {
    expect(formatCountdown(60)).toBe("1m 00s");
    expect(formatCountdown(90)).toBe("1m 30s");
    expect(formatCountdown(3599)).toBe("59m 59s");
  });

  it("keeps a ticking seconds field even past one hour", () => {
    // Regression for critique #2: the old formatter dropped seconds above
    // 60 minutes, so a long-armed countdown looked frozen minute-to-minute.
    expect(formatCountdown(3600)).toBe("1h 00m 00s");
    expect(formatCountdown(3661)).toBe("1h 01m 01s");
    expect(formatCountdown(7505)).toBe("2h 05m 05s");
  });

  it("never returns a negative value", () => {
    expect(formatCountdown(-10)).toBe("0s");
    expect(formatCountdown(Number.NaN)).toBe("0s");
  });
});

describe("formatCoarseRemaining", () => {
  it("reports sub-minute remaining as 'less than a minute'", () => {
    expect(formatCoarseRemaining(0)).toBe("less than a minute");
    expect(formatCoarseRemaining(59)).toBe("less than a minute");
  });

  it("uses singular at exactly one minute", () => {
    expect(formatCoarseRemaining(60)).toBe("about 1 minute");
    expect(formatCoarseRemaining(89)).toBe("about 1 minute");
  });

  it("rounds to the nearest whole minute and pluralises", () => {
    expect(formatCoarseRemaining(90)).toBe("about 2 minutes");
    expect(formatCoarseRemaining(900)).toBe("about 15 minutes");
    expect(formatCoarseRemaining(7200)).toBe("about 120 minutes");
  });

  it("clamps non-finite or negative input to 'less than a minute'", () => {
    expect(formatCoarseRemaining(-5)).toBe("less than a minute");
    expect(formatCoarseRemaining(Number.NaN)).toBe("less than a minute");
  });
});


describe("stabilizeDeadline", () => {
  const base = 1_700_000_000_000; // arbitrary epoch ms

  it("adopts the new deadline when there is no prior", () => {
    expect(stabilizeDeadline(base, null)).toBe(base);
  });

  it("keeps the prior deadline when the new one is within tolerance", () => {
    // +20s drift (default tolerance 45s) → keep prior, no visible jump.
    expect(stabilizeDeadline(base + 20_000, base)).toBe(base);
    // -30s drift → still within tolerance.
    expect(stabilizeDeadline(base - 30_000, base)).toBe(base);
    // exactly at the tolerance boundary → keep prior.
    expect(stabilizeDeadline(base + 45_000, base)).toBe(base);
  });

  it("adopts the new deadline when it moves beyond tolerance", () => {
    // +5min (real extend / new activity) → adopt.
    expect(stabilizeDeadline(base + 300_000, base)).toBe(base + 300_000);
    // just past the boundary.
    expect(stabilizeDeadline(base + 46_000, base)).toBe(base + 46_000);
  });

  it("respects a custom tolerance", () => {
    expect(stabilizeDeadline(base + 8_000, base, 5)).toBe(base + 8_000);
    expect(stabilizeDeadline(base + 4_000, base, 5)).toBe(base);
  });

  it("falls back to the prior deadline when the new one is non-finite", () => {
    expect(stabilizeDeadline(Number.NaN, base)).toBe(base);
  });
});
