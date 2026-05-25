import { describe, expect, it } from "vitest";

import type { BlastJobSummary } from "@/api/endpoints";
import { pickLatest, selectTopbarBlastJob } from "@/hooks/useLatestBlastJob";

function job(overrides: Partial<BlastJobSummary>): BlastJobSummary {
  return {
    job_id: "job-1",
    job_title: "blastn - core_nt",
    program: "blastn",
    db: "core_nt",
    status: "running",
    phase: "running",
    created_at: "2026-05-25T00:00:00Z",
    updated_at: "2026-05-25T00:00:00Z",
    ...overrides,
  };
}

describe("useLatestBlastJob helpers", () => {
  it("picks the row with the newest update timestamp", () => {
    const olderCompleted = job({
      job_id: "completed",
      status: "completed",
      phase: "completed",
      created_at: "2026-05-24T00:00:00Z",
      updated_at: "2026-05-24T12:00:00Z",
    });
    const newerRunning = job({
      job_id: "running",
      status: "running",
      phase: "submitting",
      created_at: "2026-05-25T01:00:00Z",
      updated_at: "2026-05-25T01:01:00Z",
    });

    expect(pickLatest([olderCompleted, newerRunning]).job_id).toBe("running");
  });

  it("prefers the active detail job over the stale latest list row", () => {
    const staleLatest = job({
      job_id: "completed",
      status: "completed",
      phase: "completed",
      updated_at: "2026-05-25T02:00:00Z",
    });
    const activeSubmitting = job({
      job_id: "active",
      status: "running",
      phase: "submitting",
      updated_at: "2026-05-25T01:00:00Z",
    });

    expect(
      selectTopbarBlastJob({
        activeJobId: "active",
        activeJob: activeSubmitting,
        jobs: [staleLatest],
      })?.job_id,
    ).toBe("active");
  });

  it("does not show an unrelated latest job while the active detail job is unresolved", () => {
    const staleLatest = job({
      job_id: "completed",
      status: "completed",
      phase: "completed",
    });

    expect(
      selectTopbarBlastJob({
        activeJobId: "active",
        jobs: [staleLatest],
      }),
    ).toBeNull();
  });
});