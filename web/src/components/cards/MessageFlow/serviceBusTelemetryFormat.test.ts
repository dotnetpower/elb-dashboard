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
