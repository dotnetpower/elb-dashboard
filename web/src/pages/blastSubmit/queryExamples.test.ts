import { describe, expect, it } from "vitest";

import { QUERY_EXAMPLE_TEMPLATES } from "@/pages/blastSubmit/queryExamples";

describe("query example templates", () => {
  it("ships valid FASTA templates with unique ids", () => {
    const ids = new Set<string>();

    for (const example of QUERY_EXAMPLE_TEMPLATES) {
      expect(ids.has(example.id)).toBe(false);
      ids.add(example.id);
      expect(example.fasta.trimStart().startsWith(">")).toBe(true);
      expect(example.length).toBeGreaterThan(0);
      expect(example.sequenceCount).toBeGreaterThan(0);
    }
  });

  it("includes multiple benchmark families", () => {
    const groups = new Set(QUERY_EXAMPLE_TEMPLATES.map((example) => example.group));

    expect(groups).toEqual(
      new Set(["Monkeypox virus", "Plasmodium falciparum", "SARS-CoV-2"]),
    );
  });
});
