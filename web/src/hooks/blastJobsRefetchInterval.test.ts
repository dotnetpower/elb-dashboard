import { describe, expect, it } from "vitest";

import type { BlastJobSummary } from "@/api/endpoints";
import {
  blastJobsRefetchInterval,
  type JobsListQueryLike,
} from "@/hooks/useScopedBlastJobs";

/** Minimal stand-in for the TanStack `Query` the interval callback reads. */
function queryWith(jobs: Partial<BlastJobSummary>[]): JobsListQueryLike {
  const withIds = jobs.map((job, i) => ({ job_id: `job-${i}`, ...job }));
  return { state: { data: { jobs: withIds as BlastJobSummary[] } } };
}

const select = blastJobsRefetchInterval({ activeMs: 5_000, idleMs: 20_000 });

describe("blastJobsRefetchInterval", () => {
  it("polls fast while a job is running", () => {
    expect(select(queryWith([{ status: "running", phase: "running" }]))).toBe(5_000);
  });

  it("polls fast while a job is queued", () => {
    expect(select(queryWith([{ status: "queued", phase: "queued" }]))).toBe(5_000);
  });

  it("polls fast when at least one of several jobs is still active", () => {
    expect(
      select(
        queryWith([
          { status: "completed", phase: "completed" },
          { status: "failed", phase: "failed" },
          { status: "running", phase: "running" },
        ]),
      ),
    ).toBe(5_000);
  });

  it("eases to the idle cadence when every job is terminal", () => {
    expect(
      select(
        queryWith([
          { status: "completed", phase: "completed" },
          { status: "failed", phase: "failed" },
        ]),
      ),
    ).toBe(20_000);
  });

  it("uses the idle cadence for an empty list", () => {
    expect(select(queryWith([]))).toBe(20_000);
  });

  it("uses the idle cadence before any data has loaded", () => {
    expect(select({ state: { data: undefined } })).toBe(20_000);
  });

  it("honours a caller-supplied idle cadence (Dashboard auto-refresh)", () => {
    const dashboard = blastJobsRefetchInterval({ activeMs: 5_000, idleMs: 60_000 });
    expect(dashboard(queryWith([{ status: "completed", phase: "completed" }]))).toBe(
      60_000,
    );
    expect(dashboard(queryWith([{ status: "running", phase: "running" }]))).toBe(5_000);
  });
});
