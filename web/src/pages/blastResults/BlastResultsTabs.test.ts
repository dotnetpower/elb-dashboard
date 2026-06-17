import { describe, expect, it } from "vitest";

import { resultTabBadge, shouldOpenRunDetailsForFailedJob } from "@/pages/blastResults/BlastResultsTabs";

describe("BLAST results tab routing", () => {
  it("opens Run details for failed jobs that deep-link to result analytics tabs", () => {
    expect(shouldOpenRunDetailsForFailedJob("descriptions", true)).toBe(true);
    expect(shouldOpenRunDetailsForFailedJob("graphic", true)).toBe(true);
    expect(shouldOpenRunDetailsForFailedJob("alignments", true)).toBe(true);
    expect(shouldOpenRunDetailsForFailedJob("taxonomy", true)).toBe(true);
  });

  it("keeps operator tabs and non-failed jobs on the requested tab", () => {
    expect(shouldOpenRunDetailsForFailedJob("files", true)).toBe(false);
    expect(shouldOpenRunDetailsForFailedJob("run", true)).toBe(false);
    expect(shouldOpenRunDetailsForFailedJob("descriptions", false)).toBe(false);
  });
});

describe("result tab in-progress badge", () => {
  it("renders a calm grey Queued badge for queued-family phases", () => {
    for (const phase of [
      "queued",
      "waiting_for_submit_slot",
      "waiting_for_capacity",
      "capacity_reserve_lost",
    ]) {
      expect(resultTabBadge(phase)).toEqual({
        label: "Queued",
        color: "var(--text-muted)",
      });
    }
  });

  it("renders the accent Running badge for genuinely running phases", () => {
    for (const phase of ["submitting", "running", "exporting_results", ""]) {
      expect(resultTabBadge(phase)).toEqual({
        label: "Running",
        color: "var(--accent)",
      });
    }
  });
});