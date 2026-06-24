/**
 * Tests for jobSubmissionSource — the single source of truth shared by the
 * JobRow User column and the Recent searches source filter.
 */
import { describe, expect, it } from "vitest";

import type { BlastJobSummary } from "@/api/endpoints";

import { jobSourceLabel, jobSubmissionSource } from "./jobSource";

function job(partial: Partial<BlastJobSummary>): BlastJobSummary {
  return { job_id: "j1", ...partial } as BlastJobSummary;
}

describe("jobSubmissionSource", () => {
  it("maps explicit servicebus payload source", () => {
    expect(jobSubmissionSource(job({ payload: { submission_source: "servicebus" } }))).toBe(
      "servicebus",
    );
  });

  it("prefers the top-level submission_source column (list rows omit payload)", () => {
    // The Jobs list reads columns only (include_payload=false), so a queue
    // job's origin lives on the top-level field, not in payload.
    expect(jobSubmissionSource(job({ submission_source: "servicebus" }))).toBe("servicebus");
    expect(jobSubmissionSource(job({ submission_source: "external_api" }))).toBe("api");
  });

  it("maps external_api payload source to api", () => {
    expect(jobSubmissionSource(job({ payload: { submission_source: "external_api" } }))).toBe(
      "api",
    );
  });

  it("treats legacy owner_upn=api as api", () => {
    expect(jobSubmissionSource(job({ owner_upn: "api" }))).toBe("api");
  });

  it("defaults to ui for dashboard submits", () => {
    expect(jobSubmissionSource(job({ payload: { submission_source: "dashboard" } }))).toBe("ui");
    expect(jobSubmissionSource(job({ owner_upn: "alice@example.com" }))).toBe("ui");
    expect(jobSubmissionSource(job({}))).toBe("ui");
  });

  it("labels servicebus as queue", () => {
    expect(jobSourceLabel("servicebus")).toBe("queue");
    expect(jobSourceLabel("api")).toBe("api (dashboard)");
    expect(jobSourceLabel("ui")).toBe("ui");
  });

  it("distinguishes control-plane queue sends from external ones", () => {
    expect(jobSourceLabel("servicebus", "control_plane")).toBe("queue (dashboard)");
    expect(jobSourceLabel("servicebus", "external")).toBe("queue");
    expect(jobSourceLabel("servicebus", "")).toBe("queue");
    expect(jobSourceLabel("servicebus", undefined)).toBe("queue");
    // queue_origin never upgrades a non-queue source.
    expect(jobSourceLabel("api", "control_plane")).toBe("api (dashboard)");
    expect(jobSourceLabel("ui", "control_plane")).toBe("ui");
  });
});
