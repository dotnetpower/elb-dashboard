import { describe, expect, it } from "vitest";

import {
  classifyJobState,
  isDashboardJobActive,
  isDashboardJobCompleted,
  isDashboardJobFailed,
  isDashboardJobQueued,
  isDashboardJobRunning,
  toJobRowView,
} from "./jobMapping";
import type { BlastJobSummary } from "@/api/endpoints";

describe("classifyJobState", () => {
  it("lets canonical running status override submitted phase", () => {
    expect(classifyJobState({ phase: "submitted", status: "running" })).toBe("Running");
  });

  it("keeps terminal failure ahead of running status", () => {
    expect(classifyJobState({ phase: "failed", status: "running" })).toBe("Failed");
  });

  it("falls back to status when phase is not recognised", () => {
    expect(classifyJobState({ phase: "mystery_phase", status: "running" })).toBe(
      "Running",
    );
  });

  it("surfaces a queued job as Queued even though the backend keeps status=running", () => {
    // submit_task.py persists status="running" as a reconciler sentinel
    // while a job waits for a submit lock / capacity gate. The phase is the
    // truth, so the row must say Queued, not Running.
    expect(
      classifyJobState({ phase: "waiting_for_submit_slot", status: "running" }),
    ).toBe("Queued");
    expect(classifyJobState({ phase: "waiting_for_capacity", status: "running" })).toBe(
      "Queued",
    );
    expect(classifyJobState({ phase: "capacity_reserve_lost", status: "running" })).toBe(
      "Queued",
    );
    expect(classifyJobState({ phase: "queued", status: "queued" })).toBe("Queued");
  });

  it("keeps a terminal outcome ahead of a queued phase", () => {
    // A late failure/completion must still win over a stale queued phase.
    expect(classifyJobState({ phase: "waiting_for_submit_slot", status: "failed" })).toBe(
      "Failed",
    );
    expect(
      classifyJobState({ phase: "waiting_for_capacity", status: "completed" }),
    ).toBe("Completed");
  });

  it("classifies submit_failed as failed even without an error string", () => {
    expect(classifyJobState({ phase: "submit_failed", status: "failed" })).toBe("Failed");
  });

  it("treats cancel-in-flight as Pending so the card stops claiming Running", () => {
    // Backend keeps status="running" while the cancel task retries so the
    // reconciler doesn't race it. phase="cancelling" must still win in
    // the UI — see api/tasks/blast/cancel_task.py.
    expect(classifyJobState({ phase: "cancelling", status: "running" })).toBe("Pending");
  });

  it("treats terminal cancel failure phases as Failed", () => {
    // The cancel task gives up after retries with phase="cancel_unavailable"
    // / status="failed" (or status="running" if the final _update_state
    // raced the row write). Either combination must surface as Failed
    // so the cluster card flips off "Running" immediately.
    expect(classifyJobState({ phase: "cancel_unavailable", status: "failed" })).toBe(
      "Failed",
    );
    expect(classifyJobState({ phase: "cancel_unavailable", status: "running" })).toBe(
      "Failed",
    );
    expect(classifyJobState({ phase: "cancel_blocked", status: "failed" })).toBe(
      "Failed",
    );
  });
});

describe("toJobRowView", () => {
  it("uses external execution summaries for split progress", () => {
    const row = toJobRowView({
      job_id: "aaaaaaaaaaaa",
      job_title: "blastn - core_nt",
      program: "blastn",
      db: "https://elbstg01.blob.core.windows.net/blast-db/core_nt/core_nt",
      status: "completed",
      phase: "completed",
      created_at: "2026-05-19T10:42:09Z",
      updated_at: "2026-05-19T10:44:14Z",
      output: {
        execution: {
          shard_count: 10,
          shards_succeeded: 9,
          shards_failed: 1,
          shards_active: 0,
        },
      },
      payload: {
        external: {
          query_file: "queries/uploads/probe/query.fa",
        },
      },
    } satisfies BlastJobSummary);

    expect(row.state).toBe("Completed");
    expect(row.title).toBe("blastn - core_nt");
    expect(row.db).toBe("core_nt");
    expect(row.query).toBe("query.fa");
    expect(row.splitsTotal).toBe(10);
    expect(row.splitsDone).toBe(10);
    expect(row.elapsedSec).toBe(125);
  });

  it("does not count stale active rows without execution as active", () => {
    const staleJob = {
      job_id: "bbbbbbbbbbbb",
      job_title: "query.fa",
      program: "blastn",
      db: "core_nt",
      status: "running",
      phase: "running",
      created_at: "2000-01-01T00:00:00Z",
      updated_at: "2000-01-01T00:00:00Z",
    } satisfies BlastJobSummary;
    const row = toJobRowView(staleJob);

    expect(row.state).toBe("Unknown");
    expect(row.splitsTotal).toBe(0);
    expect(row.note).toContain("Stale state");
    expect(isDashboardJobActive(staleJob)).toBe(false);
  });

  it("exposes shared completed and failed classifiers for all jobs surfaces", () => {
    const base = {
      job_id: "cccccccccccc",
      job_title: "query.fa",
      program: "blastn",
      db: "core_nt",
      created_at: "2026-05-19T10:42:09Z",
      updated_at: "2026-05-19T10:44:14Z",
    } satisfies Partial<BlastJobSummary>;

    expect(
      isDashboardJobCompleted({
        ...base,
        status: "completed",
        phase: "completed",
      } as BlastJobSummary),
    ).toBe(true);
    expect(
      isDashboardJobFailed({
        ...base,
        status: "failed",
        phase: "submit_failed",
      } as BlastJobSummary),
    ).toBe(true);
  });
});

describe("queued vs running classifiers", () => {
  const recent = new Date().toISOString();
  const base = {
    job_id: "dddddddddddd",
    job_title: "query.fa",
    program: "blastn",
    db: "core_nt",
    created_at: recent,
    updated_at: recent,
  } satisfies Partial<BlastJobSummary>;

  it("treats a waiting-in-line job as queued, not running", () => {
    const queued = {
      ...base,
      status: "running",
      phase: "waiting_for_capacity",
    } as BlastJobSummary;
    // Still active (in-flight) but bucketed as queued, never running.
    expect(isDashboardJobActive(queued)).toBe(true);
    expect(isDashboardJobQueued(queued)).toBe(true);
    expect(isDashboardJobRunning(queued)).toBe(false);
  });

  it("treats a job running on the cluster as running, not queued", () => {
    const running = {
      ...base,
      status: "running",
      phase: "submitted",
      output: {
        execution: {
          shard_count: 4,
          shards_succeeded: 1,
          shards_failed: 0,
          shards_active: 3,
        },
      },
    } as BlastJobSummary;
    expect(isDashboardJobRunning(running)).toBe(true);
    expect(isDashboardJobQueued(running)).toBe(false);
  });

  it("does not count a completed job as queued or running", () => {
    const done = {
      ...base,
      status: "completed",
      phase: "completed",
    } as BlastJobSummary;
    expect(isDashboardJobQueued(done)).toBe(false);
    expect(isDashboardJobRunning(done)).toBe(false);
  });
});
