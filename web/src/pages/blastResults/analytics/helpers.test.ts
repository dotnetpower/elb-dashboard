import { describe, expect, it } from "vitest";

import {
  extractCanonicalAccession,
  isNcbiAccessionLike,
  organismFromStitle,
  parseLeadingTaxid,
} from "./helpers";

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

/**
 * `isNcbiAccessionLike` gates the internal `/sequence/...` deep link in the
 * BLAST hits table. It MUST mirror the backend `_ACCESSION_RE` in
 * `api/services/ncbi/nuccore.py` so the SPA only offers the in-app viewer
 * for sseqids the backend will accept.
 */
describe("isNcbiAccessionLike", () => {
  it.each<[string, boolean]>([
    ["NM_001301717.1", true],
    ["NM_001301717", true],
    ["AB000123", true],
    ["AB000123.2", true],
    ["XP_011541223.1", true],
    ["WP_000123.1", true],
    // Pipe-delimited FASTA identifier — mirror backend `normalise_accession`.
    ["gi|123456|ref|NM_001301717.1|name", true],
    ["ref|AB000123|", true],
    // Edge cases that must NOT light up the internal link.
    ["", false],
    ["A", false],
    ["x".repeat(50), false],
    ["Query_1", false],
    ["custom_db|gene_xyz|contig_99", false],
    ["12345", false],
    [".1", false],
  ])("isNcbiAccessionLike(%j) → %j", (input, expected) => {
    expect(isNcbiAccessionLike(input)).toBe(expected);
  });
});

/**
 * `extractCanonicalAccession` produces the URL slug used by both the
 * internal `/sequence/:accession` route and the external NCBI deep link.
 * It must unwrap the same pipe-delimited shapes the backend understands.
 */
describe("extractCanonicalAccession", () => {
  it.each<[string, string]>([
    ["NM_001301717.1", "NM_001301717.1"],
    ["  AB000123.2  ", "AB000123.2"],
    // Pipe-delimited: pick the last segment that looks like an accession,
    // not just the trailing free-text "name" element.
    ["gi|123456|ref|NM_001301717.1|name", "NM_001301717.1"],
    ["ref|AB000123|", "AB000123"],
    // Custom IDs fall through unchanged so the caller can still render text.
    ["Query_1", "Query_1"],
    ["", ""],
  ])("extractCanonicalAccession(%j) → %j", (input, expected) => {
    expect(extractCanonicalAccession(input)).toBe(expected);
  });
});
