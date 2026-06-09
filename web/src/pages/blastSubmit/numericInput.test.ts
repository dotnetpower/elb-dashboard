/**
 * Tests for parseNumericInput — locks in the fix for the "0 silently becomes
 * the default" and "NaN masked by `|| default`" bugs in the BLAST parameter
 * inputs.
 */
import { describe, expect, it } from "vitest";

import { parseNumericInput } from "./numericInput";

describe("parseNumericInput", () => {
  it("keeps a deliberately-entered 0 instead of falling back", () => {
    expect(parseNumericInput("0", 100)).toBe(0);
    expect(parseNumericInput("0", 0.05)).toBe(0);
  });

  it("parses normal integers and floats", () => {
    expect(parseNumericInput("250", 100)).toBe(250);
    expect(parseNumericInput("1e-10", 0.05)).toBe(1e-10);
    expect(parseNumericInput("  42  ", 100)).toBe(42);
  });

  it("falls back on an empty / whitespace field", () => {
    expect(parseNumericInput("", 100)).toBe(100);
    expect(parseNumericInput("   ", 100)).toBe(100);
  });

  it("falls back on non-numeric garbage instead of storing NaN", () => {
    expect(parseNumericInput("abc", 100)).toBe(100);
    expect(parseNumericInput("1.2.3", 0.05)).toBe(0.05);
    expect(Number.isNaN(parseNumericInput("abc", 100))).toBe(false);
  });

  it("preserves negative numbers (caller clamps separately)", () => {
    expect(parseNumericInput("-5", 100)).toBe(-5);
  });
});
