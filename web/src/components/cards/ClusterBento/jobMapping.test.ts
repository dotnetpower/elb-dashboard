import { describe, expect, it } from "vitest";

import { classifyJobState } from "./jobMapping";

describe("classifyJobState", () => {
  it("lets canonical running status override submitted phase", () => {
    expect(classifyJobState({ phase: "submitted", status: "running" })).toBe(
      "Running",
    );
  });

  it("keeps terminal failure ahead of running status", () => {
    expect(classifyJobState({ phase: "failed", status: "running" })).toBe(
      "Failed",
    );
  });

  it("falls back to status when phase is not recognised", () => {
    expect(classifyJobState({ phase: "mystery_phase", status: "running" })).toBe(
      "Running",
    );
  });

  it("classifies submit_failed as failed even without an error string", () => {
    expect(classifyJobState({ phase: "submit_failed", status: "failed" })).toBe(
      "Failed",
    );
  });
});