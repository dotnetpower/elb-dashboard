import { describe, expect, it } from "vitest";

import { QUERY_EXAMPLE_TEMPLATES } from "@/pages/blastSubmit/queryExamples";

describe("query example templates", () => {
  const expectedSourceFiles = [
    "MPXV_F3L_NC_003310.1.fasta",
    "MPXV_F3L_NC_063383.1.fasta",
    "PF_18S rRNA_NC_004325.2[473739..475887].fa",
    "PF_18S rRNA_NC_004326.2[1289601..1291692].fa",
    "PF_18S rRNA_NC_004328.3[1083551..1086055].fa",
    "PF_18S rRNA_NC_004331.3[2800004..2802154].fa",
    "PF_18S rRNA_NC_037282.1[1925779..1928358].fa",
    "SARS-CoV-2_N_NC_045512.2.fasta",
    "SARS-CoV-2_RdRP_NC_045512.2.fasta",
    "SARS-CoV-2_orf1ab_NC_045512.2.fasta",
  ];

  it("ships valid FASTA templates with unique ids", () => {
    const ids = new Set<string>();
    const sourceFiles = new Set<string>();

    for (const example of QUERY_EXAMPLE_TEMPLATES) {
      expect(ids.has(example.id)).toBe(false);
      ids.add(example.id);
      expect(sourceFiles.has(example.sourceFile)).toBe(false);
      sourceFiles.add(example.sourceFile);
      expect(example.fasta.trimStart().startsWith(">")).toBe(true);
      expect(example.length).toBeGreaterThan(0);
      expect(example.sequenceCount).toBeGreaterThan(0);
      const computedLength = example.fasta
        .split("\n")
        .filter((line) => !line.startsWith(">"))
        .join("")
        .replace(/\s/g, "").length;
      expect(computedLength).toBe(example.length);
    }
    expect(QUERY_EXAMPLE_TEMPLATES).toHaveLength(10);
    expect([...sourceFiles].sort()).toEqual([...expectedSourceFiles].sort());
  });

  it("includes multiple benchmark families", () => {
    const groups = new Set(QUERY_EXAMPLE_TEMPLATES.map((example) => example.group));

    expect(groups).toEqual(
      new Set(["Monkeypox virus", "Plasmodium falciparum", "SARS-CoV-2"]),
    );
  });
});
