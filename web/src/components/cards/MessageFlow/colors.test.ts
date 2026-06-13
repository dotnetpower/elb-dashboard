import { describe, expect, it } from "vitest";

import { aliasTone, isErrorStatus, jobTone, statusTone } from "./colors";

describe("aliasTone", () => {
  it("is deterministic for the same alias", () => {
    expect(aliasTone("jihoon@example.com")).toEqual(aliasTone("jihoon@example.com"));
  });

  it("returns a palette entry for empty alias without throwing", () => {
    const tone = aliasTone("");
    expect(tone.accent).toBeTruthy();
    expect(tone.fill).toBeTruthy();
    expect(tone.border).toBeTruthy();
  });

  it("maps different aliases independently (stable mapping)", () => {
    const a = aliasTone("alice");
    const b = aliasTone("alice");
    expect(a).toEqual(b);
  });
});

describe("statusTone", () => {
  it("returns null for in-flight states (keep submitter colour)", () => {
    expect(statusTone("queued")).toBeNull();
    expect(statusTone("pending")).toBeNull();
    expect(statusTone("running")).toBeNull();
    expect(statusTone("reducing")).toBeNull();
  });
  it("returns a fixed override tone for terminal states", () => {
    expect(statusTone("failed")?.accent).toContain("224");
    expect(statusTone("cancelled")).not.toBeNull();
    expect(statusTone("completed")?.accent).toContain("126");
  });
  it("is case-insensitive", () => {
    expect(statusTone("FAILED")).not.toBeNull();
  });
});

describe("isErrorStatus", () => {
  it("is true only for failed", () => {
    expect(isErrorStatus("failed")).toBe(true);
    expect(isErrorStatus("FAILED")).toBe(true);
    expect(isErrorStatus("running")).toBe(false);
    expect(isErrorStatus("cancelled")).toBe(false);
  });
});

describe("jobTone", () => {
  it("uses the submitter tone for in-flight jobs", () => {
    expect(jobTone("running", "alice")).toEqual(aliasTone("alice"));
  });
  it("overrides with the status tone for terminal jobs", () => {
    expect(jobTone("failed", "alice")).toEqual(statusTone("failed"));
  });
});
