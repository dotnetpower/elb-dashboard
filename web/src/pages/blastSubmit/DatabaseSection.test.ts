import { describe, expect, it } from "vitest";

import type { BlastDatabase } from "@/api/endpoints";
import { firstDatabasePathForCategory } from "@/pages/blastSubmit/DatabaseSection";

// All fixtures carry `file_count` so the readiness check (legacy fallback)
// sees them as ready. In-flight DBs are added via `copy_status` per test.
const databases: BlastDatabase[] = [
  { name: "core_nt", container: "blast-db", prefix: "core_nt", file_count: 800 },
  { name: "nt", container: "blast-db", prefix: "nt", file_count: 900 },
  {
    name: "16S_ribosomal_RNA",
    container: "blast-db",
    prefix: "16S_ribosomal_RNA",
    file_count: 12,
  },
  { name: "wgs", container: "blast-db", prefix: "wgs", file_count: 200 },
  {
    name: "lab_panel",
    container: "custom-db",
    prefix: "users/lab_panel",
    source: "custom",
    file_count: 4,
  },
];

describe("database category selection", () => {
  it("returns the first downloaded database path for a populated category", () => {
    expect(firstDatabasePathForCategory(databases, "standard")).toBe(
      "blast-db/core_nt/core_nt",
    );
    expect(firstDatabasePathForCategory(databases, "rna")).toBe(
      "blast-db/16S_ribosomal_RNA/16S_ribosomal_RNA",
    );
    expect(firstDatabasePathForCategory(databases, "genomic")).toBe("blast-db/wgs/wgs");
    expect(firstDatabasePathForCategory(databases, "custom")).toBe(
      "custom-db/users/lab_panel/lab_panel",
    );
  });

  it("returns an empty path when the category has no downloaded database", () => {
    expect(firstDatabasePathForCategory([databases[0]], "rna")).toBe("");
    expect(firstDatabasePathForCategory(undefined, "standard")).toBe("");
  });

  it("skips in-flight DBs and picks the next ready one in the same category", () => {
    // core_nt is mid-copy, nt is ready — auto-select must land on nt.
    const inFlightCoreNt: BlastDatabase[] = [
      {
        name: "core_nt",
        container: "blast-db",
        prefix: "core_nt",
        file_count: 30,
        copy_status: { phase: "copying", success: 30, total_files: 800 },
      },
      { name: "nt", container: "blast-db", prefix: "nt", file_count: 900 },
    ];
    expect(firstDatabasePathForCategory(inFlightCoreNt, "standard")).toBe(
      "blast-db/nt/nt",
    );
  });

  it("returns empty path when every DB in the category is in-flight", () => {
    const onlyInFlight: BlastDatabase[] = [
      {
        name: "core_nt",
        container: "blast-db",
        prefix: "core_nt",
        copy_status: { phase: "copying", success: 1, total_files: 800 },
      },
    ];
    expect(firstDatabasePathForCategory(onlyInFlight, "standard")).toBe("");
  });

  it("skips DBs whose update is in progress (no copy_status)", () => {
    const updating: BlastDatabase[] = [
      {
        name: "core_nt",
        container: "blast-db",
        prefix: "core_nt",
        file_count: 800,
        update_in_progress: true,
      },
      { name: "nt", container: "blast-db", prefix: "nt", file_count: 900 },
    ];
    expect(firstDatabasePathForCategory(updating, "standard")).toBe("blast-db/nt/nt");
  });
});
