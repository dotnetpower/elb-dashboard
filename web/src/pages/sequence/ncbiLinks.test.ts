import { describe, it, expect } from "vitest";

import {
  ncbiTaxonomyUrl,
  ncbiNucleotideByOrganismUrl,
  ncbiOrganismClause,
} from "./ncbiLinks";

describe("ncbiTaxonomyUrl", () => {
  it("links to the modern Datasets taxonomy browser, not the retired wwwtax.cgi", () => {
    const url = ncbiTaxonomyUrl(2697049);
    expect(url).toBe("https://www.ncbi.nlm.nih.gov/datasets/taxonomy/2697049/");
    expect(url).not.toContain("wwwtax.cgi");
  });

  it("accepts a string taxid", () => {
    expect(ncbiTaxonomyUrl("9606")).toBe(
      "https://www.ncbi.nlm.nih.gov/datasets/taxonomy/9606/",
    );
  });
});

describe("ncbiNucleotideByOrganismUrl", () => {
  it("prefers the taxid-scoped Entrez query when a taxid is known", () => {
    const url = ncbiNucleotideByOrganismUrl({
      taxid: 2697049,
      organism: "Severe acute respiratory syndrome coronavirus 2",
    });
    // Matches the canonical form NCBI itself generates for "all nucleotide
    // sequences" on the Datasets taxonomy page.
    expect(url).toBe(
      "https://www.ncbi.nlm.nih.gov/nuccore/?term=" +
        encodeURIComponent("txid2697049[Organism:exp]"),
    );
  });

  it("quotes a multi-word organism phrase when no taxid resolved", () => {
    const url = ncbiNucleotideByOrganismUrl({
      taxid: null,
      organism: "Severe acute respiratory syndrome coronavirus 2",
    });
    // The phrase is quoted so Entrez binds [Organism] to the whole name rather
    // than only the trailing token (the bug that produced "Search failed!").
    expect(url).toBe(
      "https://www.ncbi.nlm.nih.gov/nuccore/?term=" +
        encodeURIComponent('"Severe acute respiratory syndrome coronavirus 2"[Organism]'),
    );
    expect(url).not.toContain("[orgn]");
  });

  it("trims surrounding whitespace from the organism name", () => {
    const url = ncbiNucleotideByOrganismUrl({ taxid: null, organism: "  Homo sapiens  " });
    expect(url).toBe(
      "https://www.ncbi.nlm.nih.gov/nuccore/?term=" +
        encodeURIComponent('"Homo sapiens"[Organism]'),
    );
  });

  it("returns null when neither taxid nor organism is available", () => {
    expect(ncbiNucleotideByOrganismUrl({ taxid: null, organism: null })).toBeNull();
    expect(ncbiNucleotideByOrganismUrl({ taxid: null, organism: "   " })).toBeNull();
  });
});

describe("ncbiOrganismClause", () => {
  it("returns a taxid-scoped clause when a taxid is known", () => {
    expect(ncbiOrganismClause({ taxid: 9606, organism: "Homo sapiens" })).toBe(
      "txid9606[Organism:exp]",
    );
  });

  it("quotes the organism name when no taxid is known", () => {
    expect(
      ncbiOrganismClause({ taxid: null, organism: "Homo sapiens" }),
    ).toBe('"Homo sapiens"[Organism]');
  });

  it("returns null when nothing is available", () => {
    expect(ncbiOrganismClause({ taxid: null, organism: null })).toBeNull();
    expect(ncbiOrganismClause({ taxid: null, organism: "  " })).toBeNull();
  });
});
