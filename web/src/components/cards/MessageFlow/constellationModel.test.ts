import { describe, expect, it } from "vitest";

import type { MessageFlowBox } from "@/api/messageFlow";

import { ageStyle, bornMs, jobRadius, jobTooltip, producerKind, spread01 } from "./constellationModel";

describe("producerKind", () => {
  it("is user only when a dashboard (interactive) source contributed", () => {
    expect(producerKind(["dashboard"])).toBe("user");
    expect(producerKind(["dashboard", "external_api"])).toBe("user");
  });
  it("is api for every non-interactive source", () => {
    expect(producerKind(["external_api"])).toBe("api");
    expect(producerKind(["servicebus"])).toBe("api");
    expect(producerKind([])).toBe("api");
    expect(producerKind(undefined)).toBe("api");
  });
});

describe("jobRadius", () => {
  it("returns the minimum radius when the query size is unknown", () => {
    expect(jobRadius(null)).toBe(4);
    expect(jobRadius(undefined)).toBe(4);
    expect(jobRadius(0)).toBe(4);
    expect(jobRadius(-5)).toBe(4);
  });
  it("grows with the square root of the query size", () => {
    const small = jobRadius(1_000);
    const big = jobRadius(10_000);
    expect(big).toBeGreaterThan(small);
    // sqrt scaling, not linear (10_000 letters → radius below the 18px cap).
    expect(jobRadius(10_000)).toBeCloseTo(3.5 + Math.sqrt(10_000) / 9, 5);
  });
  it("caps the radius so a pathological query cannot dominate the canvas", () => {
    expect(jobRadius(1_000_000_000)).toBe(18);
    expect(jobRadius(Number.MAX_SAFE_INTEGER)).toBe(18);
  });
});

describe("bornMs", () => {
  it("parses an ISO timestamp to epoch ms", () => {
    expect(bornMs("2026-06-13T00:00:00.000Z")).toBe(Date.parse("2026-06-13T00:00:00.000Z"));
  });
  it("returns null for missing or invalid input", () => {
    expect(bornMs(null)).toBeNull();
    expect(bornMs(undefined)).toBeNull();
    expect(bornMs("")).toBeNull();
    expect(bornMs("not-a-date")).toBeNull();
  });
});

describe("ageStyle", () => {
  const now = 1_000_000;
  it("uses a neutral middle style when born is unknown", () => {
    expect(ageStyle(null, now)).toEqual({ w: 1.1, op: 0.22 });
  });
  it("is brightest/thickest for recent links (<10s)", () => {
    expect(ageStyle(now - 5_000, now)).toEqual({ w: 1.8, op: 0.42 });
  });
  it("fades through the 10s and 30s thresholds", () => {
    expect(ageStyle(now - 20_000, now)).toEqual({ w: 1.2, op: 0.24 });
    expect(ageStyle(now - 60_000, now)).toEqual({ w: 0.8, op: 0.12 });
  });
});

describe("spread01", () => {
  it("is deterministic and centred in (-0.5, 0.5)", () => {
    const a = spread01("job-abc");
    expect(spread01("job-abc")).toBe(a);
    expect(a).toBeGreaterThanOrEqual(-0.5);
    expect(a).toBeLessThan(0.5);
  });
  it("differs for different ids (spreads a group)", () => {
    expect(spread01("job-1")).not.toBe(spread01("job-2"));
  });
});

describe("jobTooltip", () => {
  const box = (overrides: Partial<MessageFlowBox> = {}): MessageFlowBox => ({
    job_id: "job-1",
    program: "blastn",
    db: "core_nt",
    status: "running",
    phase: "search",
    query_label: null,
    query_size: 1_500,
    alias: "api-gateway",
    submission_source: "external_api",
    cluster_name: "elb-cluster-01",
    created_at: null,
    ...overrides,
  });
  it("includes program, status/phase, db, submitter and cluster", () => {
    const t = jobTooltip(box());
    expect(t).toContain("blastn");
    expect(t).toContain("running (search)");
    expect(t).toContain("db: core_nt");
    expect(t).toContain("submitter: api-gateway");
    expect(t).toContain("cluster: elb-cluster-01");
    expect(t).toContain("click to view job JSON");
  });
  it("falls back gracefully when fields are missing", () => {
    const t = jobTooltip(box({ program: null, db: null, phase: null, cluster_name: "" }));
    expect(t).toContain("blast ·");
    expect(t).toContain("cluster: unassigned");
    expect(t).not.toContain("db:");
  });
  it("notes a settling (fading) job and surfaces its error code", () => {
    const t = jobTooltip(box({ status: "failed", lifecycle: "settling", error_code: "database_not_found" }));
    expect(t).toContain("(finishing — fading out)");
    expect(t).toContain("error: database_not_found");
  });
});
