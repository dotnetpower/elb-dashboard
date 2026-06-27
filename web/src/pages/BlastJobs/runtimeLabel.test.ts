/**
 * Tests for runtimeLabel — the BlastJobs row "Queued for / Elapsed / Duration"
 * timer. Pins the anchor-switch contract introduced in 2507352:
 *
 *   queued                 -> anchor = created_at  (label: "Queued for")
 *   active (non-queued)    -> anchor = started_at ?? created_at  (label: "Elapsed")
 *   terminal               -> anchor = started_at ?? created_at  (label: "Duration")
 *
 * If the anchor falls back to created_at when started_at is present, the
 * "Elapsed" / "Duration" counter folds queue-wait into runtime — exactly the
 * regression Option C set out to fix.
 */
import { describe, expect, it } from "vitest";

import type { BlastJobSummary } from "@/api/endpoints";

import { runtimeLabel } from "./JobRow";

const QUEUED_AT = "2026-06-27T05:00:00.000Z"; // t0
const STARTED_AT = "2026-06-27T05:30:00.000Z"; // t0 + 1800s queue-wait
const FINISHED_AT = "2026-06-27T05:35:00.000Z"; // started + 300s run

const NOW_DURING_RUN = Date.parse("2026-06-27T05:33:00.000Z"); // started + 180s
const NOW_ANY = Date.parse("2026-06-27T06:00:00.000Z"); // arbitrary; queued ignores

function job(partial: Partial<BlastJobSummary>): BlastJobSummary {
  return {
    job_id: "j1",
    created_at: QUEUED_AT,
    ...partial,
  } as BlastJobSummary;
}

describe("runtimeLabel — queued state", () => {
  it("uses created_at anchor and labels 'Queued for'", () => {
    const result = runtimeLabel(job({}), "Queued", NOW_ANY);
    // NOW_ANY - QUEUED_AT = 1h = 3600s -> "1h 0m"
    expect(result).toEqual({ label: "Queued for", value: "1h 0m" });
  });

  it("ignores started_at when the row is still queued (Queued for stays on created_at)", () => {
    // started_at on a queued row should be impossible in practice, but if it
    // ever appears (race between sibling snapshot + dashboard projection), the
    // queued branch MUST stay anchored to created_at so the badge label
    // matches the displayed duration.
    const result = runtimeLabel(job({ started_at: STARTED_AT }), "Queued", NOW_ANY);
    expect(result).toEqual({ label: "Queued for", value: "1h 0m" });
  });
});

describe("runtimeLabel — active (non-queued) state", () => {
  it("anchors on started_at when present and labels 'Elapsed'", () => {
    // NOW_DURING_RUN - STARTED_AT = 180s -> "3m 0s" (queue-wait excluded)
    const result = runtimeLabel(
      job({ started_at: STARTED_AT, updated_at: STARTED_AT }),
      "Running",
      NOW_DURING_RUN,
    );
    expect(result).toEqual({ label: "Elapsed", value: "3m 0s" });
  });

  it("falls back to created_at when started_at is missing (legacy / pre-sibling row)", () => {
    // NOW_DURING_RUN - QUEUED_AT = 33min = 1980s -> "33m 0s" (the old buggy
    // behaviour for every row, kept only as a fallback for rows the backend
    // has not yet stamped a started_at on).
    const result = runtimeLabel(
      job({ updated_at: QUEUED_AT }),
      "Running",
      NOW_DURING_RUN,
    );
    expect(result).toEqual({ label: "Elapsed", value: "33m 0s" });
  });

  it("treats an unparseable started_at as missing and falls back to created_at", () => {
    const result = runtimeLabel(
      job({ started_at: "not-a-date", updated_at: QUEUED_AT }),
      "Running",
      NOW_DURING_RUN,
    );
    expect(result).toEqual({ label: "Elapsed", value: "33m 0s" });
  });
});

describe("runtimeLabel — terminal state", () => {
  it("computes Duration from started_at -> updated_at, excluding queue wait", () => {
    // FINISHED_AT - STARTED_AT = 300s -> "5m 0s"
    const result = runtimeLabel(
      job({ started_at: STARTED_AT, updated_at: FINISHED_AT }),
      "Completed",
      NOW_ANY, // ignored for terminal: end = updated_at
    );
    expect(result).toEqual({ label: "Duration", value: "5m 0s" });
  });

  it("falls back to created_at -> updated_at when started_at missing", () => {
    // FINISHED_AT - QUEUED_AT = 2100s = 35min -> "35m 0s" (legacy fallback)
    const result = runtimeLabel(
      job({ updated_at: FINISHED_AT }),
      "Completed",
      NOW_ANY,
    );
    expect(result).toEqual({ label: "Duration", value: "35m 0s" });
  });

  it("labels failed rows with 'Duration' (terminal-state group)", () => {
    const result = runtimeLabel(
      job({ started_at: STARTED_AT, updated_at: FINISHED_AT }),
      "Failed",
      NOW_ANY,
    );
    expect(result?.label).toBe("Duration");
  });
});

describe("runtimeLabel — defensive guards", () => {
  it("returns null when created_at is unparseable", () => {
    const result = runtimeLabel(
      job({ created_at: "" as unknown as string }),
      "Completed",
      NOW_ANY,
    );
    expect(result).toBeNull();
  });

  it("returns null when end-of-range is before begin (clock skew / data glitch)", () => {
    // updated_at BEFORE started_at would yield negative duration -- the row
    // must render nothing rather than a misleading "0s" or absolute value.
    const result = runtimeLabel(
      job({
        started_at: FINISHED_AT,
        updated_at: STARTED_AT, // earlier than started_at
      }),
      "Completed",
      NOW_ANY,
    );
    expect(result).toBeNull();
  });
});
