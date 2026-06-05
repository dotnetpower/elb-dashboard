import { describe, expect, it } from "vitest";

import type { BlastDatabase } from "@/api/endpoints";
import {
  GOAL_OPTIONS,
  readyPathForSuggestion,
} from "@/pages/blastSubmit/DatabaseRecommendPanel";

// Mirror of the backend SUPPORTED_GOALS tuple in
// api/services/blast/db_recommendation.py. The panel dropdown must stay in
// sync with the oracle's accepted goals or a selection would silently fall
// back to "identify" on the server.
const BACKEND_SUPPORTED_GOALS = [
  "identify",
  "highly_similar",
  "transcripts",
  "genomes",
  "well_characterized",
  "comprehensive",
];

const databases: BlastDatabase[] = [
  { name: "core_nt", container: "blast-db", prefix: "core_nt", file_count: 800 },
  {
    name: "nt",
    container: "blast-db",
    prefix: "nt",
    file_count: 30,
    copy_status: { phase: "copying", success: 30, total_files: 900 },
  },
];

describe("readyPathForSuggestion", () => {
  it("returns the storage path for a downloaded + ready database", () => {
    expect(readyPathForSuggestion(databases, "core_nt")).toBe("blast-db/core_nt/core_nt");
  });

  it("returns null when the suggested database is mid-copy (not ready)", () => {
    expect(readyPathForSuggestion(databases, "nt")).toBeNull();
  });

  it("returns null when the suggested database is not downloaded", () => {
    expect(readyPathForSuggestion(databases, "swissprot")).toBeNull();
    expect(readyPathForSuggestion(undefined, "core_nt")).toBeNull();
  });
});

describe("GOAL_OPTIONS", () => {
  it("exactly covers the backend SUPPORTED_GOALS enum", () => {
    expect(GOAL_OPTIONS.map((option) => option.value)).toEqual(BACKEND_SUPPORTED_GOALS);
  });

  it("gives every goal a non-empty researcher-facing label", () => {
    for (const option of GOAL_OPTIONS) {
      expect(option.label.length).toBeGreaterThan(0);
    }
  });
});
