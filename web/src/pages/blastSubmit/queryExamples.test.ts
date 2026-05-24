import { describe, expect, it } from "vitest";

import {
  QUERY_EXAMPLE_TEMPLATES,
  queryExamplesForDatabase,
} from "@/pages/blastSubmit/queryExamples";
import { DB_DESCRIPTIONS } from "@/pages/blastSubmitModel";

describe("query example templates", () => {
  const expectedTemplateCount = 30;

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
    expect(QUERY_EXAMPLE_TEMPLATES).toHaveLength(expectedTemplateCount);
  });

  it("includes multiple benchmark families", () => {
    const groups = new Set(QUERY_EXAMPLE_TEMPLATES.map((example) => example.group));

    expect([...groups]).toEqual(
      expect.arrayContaining([
        "Monkeypox virus",
        "Plasmodium falciparum",
        "SARS-CoV-2",
        "Escherichia coli",
        "Bacillus subtilis",
        "Staphylococcus aureus",
        "Pseudomonas aeruginosa",
        "Mycobacterium tuberculosis",
        "Saccharomyces cerevisiae",
        "Candida albicans",
        "Aspergillus fumigatus",
        "Cryptococcus neoformans",
        "Penicillium chrysogenum",
        "PDB structural RNA",
        "Human proteins",
        "SARS-CoV-2 proteins",
      ]),
    );
  });

  it("tags every example with a known program and at least one known database", () => {
    const knownPrograms = new Set(["blastn", "blastp", "blastx", "tblastn", "tblastx"]);
    const knownDbs = new Set(Object.keys(DB_DESCRIPTIONS));
    for (const example of QUERY_EXAMPLE_TEMPLATES) {
      expect(knownPrograms.has(example.blastProgram)).toBe(true);
      expect(example.matchingDbs.length).toBeGreaterThan(0);
      for (const db of example.matchingDbs) {
        expect(knownDbs.has(db)).toBe(true);
      }
    }
  });

  it("covers every database in DB_DESCRIPTIONS with at least one example", () => {
    const covered = new Set<string>();
    for (const example of QUERY_EXAMPLE_TEMPLATES) {
      for (const db of example.matchingDbs) covered.add(db);
    }
    for (const db of Object.keys(DB_DESCRIPTIONS)) {
      expect(covered.has(db)).toBe(true);
    }
  });

  it("provides at least five curated examples for each built-in database", () => {
    for (const db of Object.keys(DB_DESCRIPTIONS)) {
      expect(
        queryExamplesForDatabase(QUERY_EXAMPLE_TEMPLATES, db).length,
      ).toBeGreaterThanOrEqual(5);
    }
  });

  it("uses nucleotide programs for nucleotide DBs and protein programs for protein DBs", () => {
    for (const example of QUERY_EXAMPLE_TEMPLATES) {
      const isProteinExample = example.blastProgram === "blastp";
      for (const dbName of example.matchingDbs) {
        const meta = DB_DESCRIPTIONS[dbName];
        if (!meta) continue;
        if (isProteinExample) {
          expect(meta.type).toBe("prot");
        } else if (example.blastProgram === "blastn") {
          expect(meta.type).toBe("nucl");
        }
      }
    }
  });

  it("returns only examples tagged for the selected database", () => {
    const sixteenS = queryExamplesForDatabase(
      QUERY_EXAMPLE_TEMPLATES,
      "16S_ribosomal_RNA",
    );
    expect(sixteenS.map((e) => e.id)).toEqual(
      expect.arrayContaining(["ecoli-16S-nr-024570", "bacillus-subtilis-16s-nr-102783"]),
    );
    expect(sixteenS).toHaveLength(5);

    const its = queryExamplesForDatabase(QUERY_EXAMPLE_TEMPLATES, "ITS_RefSeq_Fungi");
    expect(its.map((e) => e.id)).toEqual(
      expect.arrayContaining([
        "scerevisiae-its-nr-111007",
        "aspergillus-fumigatus-its-pz411633",
      ]),
    );
    expect(its).toHaveLength(5);

    const pdbnt = queryExamplesForDatabase(QUERY_EXAMPLE_TEMPLATES, "pdbnt");
    expect(pdbnt.map((e) => e.id)).toEqual(
      expect.arrayContaining(["trna-phe-1ehz", "pdb-2gdi-tpp-riboswitch"]),
    );
    expect(pdbnt).toHaveLength(5);

    expect(
      queryExamplesForDatabase(QUERY_EXAMPLE_TEMPLATES, "swissprot").map(
        (e) => e.blastProgram,
      ),
    ).toEqual(["blastp", "blastp", "blastp", "blastp", "blastp"]);
    expect(queryExamplesForDatabase(QUERY_EXAMPLE_TEMPLATES, "custom_lab_db")).toEqual(
      [],
    );
    expect(queryExamplesForDatabase(QUERY_EXAMPLE_TEMPLATES, "")).toEqual([]);
  });
});
