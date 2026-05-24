import { describe, expect, it } from "vitest";

import type { BlastDatabase } from "@/api/endpoints";
import { firstDatabasePathForCategory } from "@/pages/blastSubmit/DatabaseSection";

const databases: BlastDatabase[] = [
  { name: "core_nt", container: "blast-db", prefix: "core_nt" },
  { name: "nt", container: "blast-db", prefix: "nt" },
  { name: "16S_ribosomal_RNA", container: "blast-db", prefix: "16S_ribosomal_RNA" },
  { name: "wgs", container: "blast-db", prefix: "wgs" },
  {
    name: "lab_panel",
    container: "custom-db",
    prefix: "users/lab_panel",
    source: "custom",
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
});
