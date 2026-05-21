import { describe, expect, it } from "vitest";

import { organismFromStitle, parseLeadingTaxid } from "./helpers";

/**
 * Mirrors the backend test
 * `api/tests/test_blast_result_analytics_organism.py::test_extract_organism_from_stitle`
 * so the frontend "Scientific Name" column matches what the server-side
 * Taxonomy rollup falls back to.
 */
describe("organismFromStitle", () => {
  it.each([
    [
      "Monkeypox virus isolate 24MPX2634V genome assembly, complete genome",
      "Monkeypox virus",
    ],
    ["Homo sapiens chromosome 7, GRCh38 reference", "Homo sapiens"],
    ["Escherichia coli strain K-12 complete genome", "Escherichia coli"],
    [
      "Severe acute respiratory syndrome coronavirus 2 isolate Wuhan-Hu-1",
      "Severe acute respiratory syndrome coronavirus 2",
    ],
    [
      "PREDICTED: Mus musculus uncharacterized LOC123 (Loc123), mRNA",
      "Mus musculus uncharacterized LOC123",
    ],
    [
      "Saccharomyces cerevisiae S288C chromosome IV, complete sequence",
      "Saccharomyces cerevisiae S288C",
    ],
    ["Drosophila melanogaster", "Drosophila melanogaster"],
    ["", ""],
    ["   ", ""],
    // No confident candidate — too many tokens.
    [
      "Some very long marketing tagline with no scientific name at all that goes on and on",
      "",
    ],
  ])("extracts %j → %j", (input, expected) => {
    expect(organismFromStitle(input)).toBe(expected);
  });

  it("returns empty for nullish input", () => {
    expect(organismFromStitle(undefined)).toBe("");
  });
});

/**
 * The Scientific Name modal opens via `parseLeadingTaxid(hit.staxids)` so
 * that callers can skip the name-→taxid lookup when the BLAST row already
 * carries a numeric taxid. The parser must be lenient about whitespace and
 * mixed separators (`;`, `,`) since the upstream BLAST tab-separated output
 * is not strict about either.
 */
describe("parseLeadingTaxid", () => {
  it.each<[string | null | undefined, number | null]>([
    ["10244", 10244],
    ["10244;9606", 10244],
    ["10244,9606", 10244],
    [" 10244 ; 9606 ", 10244],
    ["", null],
    [null, null],
    [undefined, null],
    [";9606", null],
    ["not-a-number", null],
    ["0", null],
    ["-7", null],
  ])("parses %j → %j", (input, expected) => {
    expect(parseLeadingTaxid(input)).toBe(expected);
  });
});
