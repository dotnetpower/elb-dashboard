/**
 * Unit tests for the SequenceBuilderDialog pure helpers — coordinate/strand
 * resolution and FASTA header preview. The minus-strand swap is the load-
 * bearing bit: it must reproduce NCBI's `seq_start > seq_stop` reverse-
 * complement semantics and the `:cSTOP-START` header form.
 */
import { describe, expect, it } from "vitest";

import {
  buildSubrange,
  previewFastaHeader,
} from "@/pages/blastSubmit/SequenceBuilderDialog";

describe("buildSubrange", () => {
  it("returns empty coordinates when both fields are blank", () => {
    expect(buildSubrange("", "", "plus")).toEqual({});
  });

  it("maps plus strand to seq_start <= seq_stop", () => {
    expect(buildSubrange("46022", "46483", "plus")).toEqual({
      seqStart: 46022,
      seqStop: 46483,
    });
  });

  it("swaps coordinates for the minus strand (reverse-complement)", () => {
    expect(buildSubrange("46022", "46483", "minus")).toEqual({
      seqStart: 46483,
      seqStop: 46022,
    });
  });

  it("normalises reversed input so the strand toggle wins (plus)", () => {
    // Bounds typed high→low but strand=plus must still fetch low→high.
    expect(buildSubrange("46483", "46022", "plus")).toEqual({
      seqStart: 46022,
      seqStop: 46483,
    });
  });

  it("normalises reversed input so the strand toggle wins (minus)", () => {
    expect(buildSubrange("46022", "46483", "minus")).toEqual({
      seqStart: 46483,
      seqStop: 46022,
    });
    // Same result whichever order the bounds were typed.
    expect(buildSubrange("46483", "46022", "minus")).toEqual({
      seqStart: 46483,
      seqStop: 46022,
    });
  });

  it("errors when only one bound is supplied", () => {
    expect(buildSubrange("100", "", "plus").error).toBeTruthy();
    expect(buildSubrange("", "200", "plus").error).toBeTruthy();
  });

  it("errors on non-positive or non-integer values", () => {
    expect(buildSubrange("0", "10", "plus").error).toBeTruthy();
    expect(buildSubrange("1.5", "10", "plus").error).toBeTruthy();
    expect(buildSubrange("-3", "10", "plus").error).toBeTruthy();
  });
});

describe("previewFastaHeader", () => {
  it("is empty without an accession", () => {
    expect(previewFastaHeader("", "1", "2", "plus")).toBe("");
  });

  it("labels the whole sequence when no sub-range", () => {
    expect(previewFastaHeader("NC_063383.1", "", "", "plus")).toBe(
      ">NC_063383.1 (whole sequence)",
    );
  });

  it("renders the minus strand as :cSTOP-START (matches the F3L query)", () => {
    expect(previewFastaHeader("NC_063383.1", "46022", "46483", "minus")).toBe(
      ">NC_063383.1:c46483-46022",
    );
  });

  it("renders the plus strand as :START-STOP", () => {
    expect(previewFastaHeader("NC_063383.1", "100", "600", "plus")).toBe(
      ">NC_063383.1:100-600",
    );
  });
});
