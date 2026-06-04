import { describe, expect, it } from "vitest";

import {
  computeWindowedBytesPerSec,
  computeWindowedSpeed,
  formatDuration,
  formatEta,
  formatEtaFromBytes,
  formatSpeed,
  recordSpeedSample,
  SPEED_WINDOW_MS,
  type SpeedSample,
} from "@/components/cards/storage/blastDbProgress";

describe("formatDuration", () => {
  it("renders seconds under a minute", () => {
    expect(formatDuration(0)).toBe("0s");
    expect(formatDuration(45)).toBe("45s");
    expect(formatDuration(59)).toBe("59s");
  });

  it("renders whole minutes under an hour", () => {
    expect(formatDuration(60)).toBe("1m");
    expect(formatDuration(150)).toBe("3m");
    expect(formatDuration(3540)).toBe("59m");
  });

  it("renders hours and minutes", () => {
    expect(formatDuration(3600)).toBe("1h");
    expect(formatDuration(3900)).toBe("1h 5m");
    expect(formatDuration(7200)).toBe("2h");
  });

  it("clamps negatives to zero", () => {
    expect(formatDuration(-10)).toBe("0s");
  });
});

describe("formatEta", () => {
  it("returns empty when there is no total", () => {
    expect(formatEta(120, 10, 0)).toBe("");
  });

  it("returns empty once every file is copied", () => {
    expect(formatEta(120, 800, 800)).toBe("");
    expect(formatEta(120, 801, 800)).toBe("");
  });

  it("reports estimating before throughput stabilises", () => {
    expect(formatEta(120, 0, 800)).toBe("estimating…");
    expect(formatEta(3, 10, 800)).toBe("estimating…");
  });

  it("projects remaining time from observed throughput", () => {
    // 10 files in 100 s → 10 s/file; 790 remaining → ~7900 s → 2h 12m.
    expect(formatEta(100, 10, 800)).toBe("~2h 12m left");
    // Half done in 600 s → ~600 s remaining → 10m.
    expect(formatEta(600, 400, 800)).toBe("~10m left");
  });
});

describe("formatSpeed", () => {
  it("returns empty when nothing has landed yet", () => {
    expect(formatSpeed(0, 100)).toBe("");
  });

  it("returns empty before the rate stabilises", () => {
    expect(formatSpeed(1024 * 1024, 3)).toBe("");
  });

  it("renders MB/s for a typical download rate", () => {
    // 100 MiB in 10 s → 10 MiB/s.
    expect(formatSpeed(100 * 1024 * 1024, 10)).toBe("10.0 MB/s");
  });

  it("scales up to GB/s and down to KB/s", () => {
    // 20 GiB in 10 s → 2 GiB/s.
    expect(formatSpeed(20 * 1024 * 1024 * 1024, 10)).toBe("2.0 GB/s");
    // 5 MiB in 10 s → 512 KiB/s (>= 100 → no decimals).
    expect(formatSpeed(5 * 1024 * 1024, 10)).toBe("512 KB/s");
    // 200 KiB in 10 s → 20.5 KiB/s (< 100 → one decimal).
    expect(formatSpeed(205 * 1024, 10)).toBe("20.5 KB/s");
  });

  it("drops decimals for large magnitudes and raw bytes", () => {
    // 1 s elapsed is below the 5 s stability gate → empty.
    expect(formatSpeed(1200 * 1024 * 1024, 1)).toBe("");
    // 300 MiB/s → no decimal (>= 100).
    expect(formatSpeed(300 * 1024 * 1024 * 10, 10)).toBe("300 MB/s");
    // Sub-KB rate → B/s with no decimal.
    expect(formatSpeed(5000, 10)).toBe("500 B/s");
  });
});

describe("recordSpeedSample", () => {
  it("appends only when bytes strictly advance", () => {
    const s0: SpeedSample[] = [];
    const s1 = recordSpeedSample(s0, 100, 1_000);
    expect(s1).toEqual([{ bytes: 100, t: 1_000 }]);
    // Same byte count (repeat render) → no new sample.
    const s2 = recordSpeedSample(s1, 100, 1_500);
    expect(s2).toEqual([{ bytes: 100, t: 1_000 }]);
    // A decrease (metadata reset) → no backwards sample.
    const s3 = recordSpeedSample(s2, 40, 2_000);
    expect(s3).toEqual([{ bytes: 100, t: 1_000 }]);
    // A real advance → appended.
    const s4 = recordSpeedSample(s3, 250, 2_500);
    expect(s4).toEqual([
      { bytes: 100, t: 1_000 },
      { bytes: 250, t: 2_500 },
    ]);
  });

  it("does not mutate the input array", () => {
    const s0: SpeedSample[] = [{ bytes: 10, t: 0 }];
    const s1 = recordSpeedSample(s0, 20, 100);
    expect(s0).toEqual([{ bytes: 10, t: 0 }]);
    expect(s1).not.toBe(s0);
  });

  it("trims samples older than the window but keeps the last two", () => {
    let samples: SpeedSample[] = [];
    samples = recordSpeedSample(samples, 10, 0);
    samples = recordSpeedSample(samples, 20, 10_000);
    samples = recordSpeedSample(samples, 30, 20_000);
    // now=70_000: both t=0 and t=10_000 are older than the 45 s window and get
    // dropped, leaving the two most recent samples.
    samples = recordSpeedSample(samples, 40, 70_000);
    expect(samples.map((s) => s.t)).toEqual([20_000, 70_000]);
  });

  it("never drops below two samples even when all are stale", () => {
    let samples: SpeedSample[] = [];
    samples = recordSpeedSample(samples, 10, 0);
    samples = recordSpeedSample(samples, 20, 1_000);
    // Far in the future: both samples are stale, but the floor keeps two.
    samples = recordSpeedSample(samples, 20, 10 * SPEED_WINDOW_MS);
    expect(samples).toEqual([
      { bytes: 10, t: 0 },
      { bytes: 20, t: 1_000 },
    ]);
  });
});

describe("computeWindowedSpeed", () => {
  it("returns empty with fewer than two distinct samples", () => {
    expect(computeWindowedSpeed([], 1_000)).toBe("");
    expect(computeWindowedSpeed([{ bytes: 100, t: 1_000 }], 1_000)).toBe("");
    // Two samples at the same instant → no measurable interval.
    expect(
      computeWindowedSpeed(
        [
          { bytes: 100, t: 1_000 },
          { bytes: 200, t: 1_000 },
        ],
        1_000,
      ),
    ).toBe("");
  });

  it("projects the instantaneous rate from the first↔last delta", () => {
    // 100 MiB landed across a 10 s window → 10 MiB/s, ignoring earlier idle.
    const samples: SpeedSample[] = [
      { bytes: 0, t: 0 },
      { bytes: 100 * 1024 * 1024, t: 10_000 },
    ];
    expect(computeWindowedSpeed(samples, 10_000)).toBe("10.0 MB/s");
  });

  it("hides the rate when the latest sample is stale", () => {
    const samples: SpeedSample[] = [
      { bytes: 0, t: 0 },
      { bytes: 100 * 1024 * 1024, t: 10_000 },
    ];
    // now is more than a window past the last advance → stalled → hidden.
    expect(computeWindowedSpeed(samples, 10_000 + SPEED_WINDOW_MS + 1)).toBe("");
  });
});

describe("computeWindowedBytesPerSec", () => {
  it("returns null with fewer than two distinct samples", () => {
    expect(computeWindowedBytesPerSec([], 1_000)).toBeNull();
    expect(
      computeWindowedBytesPerSec([{ bytes: 100, t: 1_000 }], 1_000),
    ).toBeNull();
    expect(
      computeWindowedBytesPerSec(
        [
          { bytes: 100, t: 1_000 },
          { bytes: 200, t: 1_000 },
        ],
        1_000,
      ),
    ).toBeNull();
  });

  it("computes the numeric rate from the first↔last delta", () => {
    // 50 MB across a 10 s window → 5 MB/s as a raw number.
    const samples: SpeedSample[] = [
      { bytes: 0, t: 0 },
      { bytes: 50_000_000, t: 10_000 },
    ];
    expect(computeWindowedBytesPerSec(samples, 10_000)).toBe(5_000_000);
  });

  it("returns null when the latest sample is stale", () => {
    const samples: SpeedSample[] = [
      { bytes: 0, t: 0 },
      { bytes: 50_000_000, t: 10_000 },
    ];
    expect(
      computeWindowedBytesPerSec(samples, 10_000 + SPEED_WINDOW_MS + 1),
    ).toBeNull();
  });

  it("returns null when bytes did not advance", () => {
    const samples: SpeedSample[] = [
      { bytes: 100, t: 0 },
      { bytes: 100, t: 10_000 },
    ];
    expect(computeWindowedBytesPerSec(samples, 10_000)).toBeNull();
  });
});

describe("formatEtaFromBytes", () => {
  it("returns empty without a positive rate", () => {
    expect(formatEtaFromBytes(1_000, null)).toBe("");
    expect(formatEtaFromBytes(1_000, 0)).toBe("");
    expect(formatEtaFromBytes(1_000, -5)).toBe("");
  });

  it("returns empty when nothing remains", () => {
    expect(formatEtaFromBytes(0, 1_000)).toBe("");
    expect(formatEtaFromBytes(-100, 1_000)).toBe("");
  });

  it("projects remaining time from the byte rate", () => {
    // 60 GB remaining at 37 MB/s → ~1622 s → 27m.
    expect(formatEtaFromBytes(60_000_000_000, 37_000_000)).toBe("~27m left");
    // 100 MB remaining at 10 MB/s → 10 s.
    expect(formatEtaFromBytes(100_000_000, 10_000_000)).toBe("~10s left");
  });
});

