import { describe, expect, it } from "vitest";

import { shouldOpenRunDetailsForFailedJob } from "@/pages/blastResults/BlastResultsTabs";

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