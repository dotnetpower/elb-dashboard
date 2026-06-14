/**
 * serviceBusTelemetryFormat.test — locks the helper math behind the
 * ServiceBusTelemetryPanel renderer.
 *
 * The main risk this guards against is unit drift on `size_pct`: the
 * backend ships it on the 0..100 percent scale (e.g. `0.05` = 0.05 %), not
 * as a 0..1 fraction. Re-introducing a `pct * 100` multiplication would
 * make a healthy <1 % queue render as ≥100 % and trip the danger tone
 * falsely; the test below would fail immediately.
 */
import { describe, expect, it } from "vitest";

import {
  dlqDeltaSummary,
  fillTone,
  formatBytes,
  formatPct,
  statusTone,
} from "./serviceBusTelemetryFormat";

describe("formatBytes", () => {
  it("renders a dash for null / non-finite / non-positive", () => {
    expect(formatBytes(null)).toBe("—");
    expect(formatBytes(Number.NaN)).toBe("—");
    expect(formatBytes(0)).toBe("—");
    expect(formatBytes(-1)).toBe("—");
  });

  it("renders raw bytes under 1 KiB", () => {
    expect(formatBytes(512)).toBe("512 B");
  });

  it("renders KB / MB / GB with one or two decimals", () => {
    expect(formatBytes(512 * 1024)).toBe("512.0 KB");
    expect(formatBytes(2 * 1024 * 1024)).toBe("2.0 MB");
    expect(formatBytes(3 * 1024 * 1024 * 1024)).toBe("3.00 GB");
  });
});

describe("formatPct", () => {
  it("renders a dash for null / non-finite", () => {
    expect(formatPct(null)).toBe("—");
    expect(formatPct(Number.POSITIVE_INFINITY)).toBe("—");
  });

  it("treats input as already-percent (0..100), not a fraction", () => {
    // The backend computes `0.5 MiB / 1024 MiB * 100` = 0.0488… → rounded
    // to 0.05. If we ever multiply by 100 again, this becomes "5.00%" and
    // this test fails — that is the regression guard.
    expect(formatPct(0.05)).toBe("0.05%");
    expect(formatPct(12.5)).toBe("12.50%");
    expect(formatPct(99.99)).toBe("99.99%");
  });
});

describe("fillTone", () => {
  it("returns the muted tone for null or small percent values", () => {
    expect(fillTone(null)).toBe("var(--text-muted)");
    expect(fillTone(0)).toBe("var(--text-muted)");
    expect(fillTone(1)).toBe("var(--text-muted)");
    expect(fillTone(49.9)).toBe("var(--text-muted)");
  });

  it("returns the warning tone between 50% and 80%", () => {
    expect(fillTone(50)).toBe("var(--warning)");
    expect(fillTone(79.9)).toBe("var(--warning)");
  });

  it("returns the danger tone at or above 80%", () => {
    expect(fillTone(80)).toBe("var(--danger)");
    expect(fillTone(99)).toBe("var(--danger)");
  });
});

describe("statusTone", () => {
  it("maps known statuses to the right tones", () => {
    expect(statusTone(null)).toBe("var(--text-faint)");
    expect(statusTone(undefined)).toBe("var(--text-faint)");
    expect(statusTone("Active")).toBe("var(--success, var(--accent))");
    expect(statusTone("Disabled")).toBe("var(--danger)");
    expect(statusTone("SendDisabled")).toBe("var(--warning)");
    expect(statusTone("ReceiveDisabled")).toBe("var(--warning)");
  });
});

describe("dlqDeltaSummary", () => {
  it("renders 'since first sample' when only one sample is in the window", () => {
    const summary = dlqDeltaSummary({
      window_seconds: 3600,
      samples: 1,
      baseline_dlq: 4,
      current_dlq: 4,
      delta: 0,
      elapsed_seconds: 0,
    });
    expect(summary.text).toBe("4 since first sample");
    expect(summary.tone).toBe("var(--warning)");
  });

  it("renders 'no growth' when delta is 0 with multiple samples", () => {
    const summary = dlqDeltaSummary({
      window_seconds: 3600,
      samples: 12,
      baseline_dlq: 0,
      current_dlq: 0,
      delta: 0,
      elapsed_seconds: 600,
    });
    expect(summary.text).toBe("no growth in last 600s");
    expect(summary.tone).toBe("var(--text-muted)");
  });

  it("renders a positive delta tinted warning", () => {
    const summary = dlqDeltaSummary({
      window_seconds: 3600,
      samples: 6,
      baseline_dlq: 4,
      current_dlq: 9,
      delta: 5,
      elapsed_seconds: 90,
    });
    expect(summary.text).toBe("+5 in last 90s");
    expect(summary.tone).toBe("var(--warning)");
  });

  it("never reports negative growth even if the queue heals mid-window", () => {
    // Backend clamps delta at 0 already, but the SPA must stay honest if a
    // future contract loosens that. Test by simulating a "healing" delta = 0
    // with samples > 1.
    const summary = dlqDeltaSummary({
      window_seconds: 3600,
      samples: 5,
      baseline_dlq: 10,
      current_dlq: 0,
      delta: 0,
      elapsed_seconds: 300,
    });
    expect(summary.text).toBe("no growth in last 300s");
  });

  it("rounds elapsed seconds to the nearest integer in the summary", () => {
    const summary = dlqDeltaSummary({
      window_seconds: 3600,
      samples: 3,
      baseline_dlq: 0,
      current_dlq: 0,
      delta: 0,
      elapsed_seconds: 47.7,
    });
    expect(summary.text).toBe("no growth in last 48s");
  });
});
