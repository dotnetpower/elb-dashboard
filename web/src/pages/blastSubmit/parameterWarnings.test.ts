import { describe, expect, it } from "vitest";

import { INITIAL } from "@/pages/blastSubmitModel";
import { parameterWarnings } from "@/pages/blastSubmit/parameterWarnings";

describe("parameterWarnings", () => {
  it("returns no warning for the recommended defaults", () => {
    expect(parameterWarnings({ ...INITIAL })).toEqual([]);
    expect(parameterWarnings({ ...INITIAL, word_size: "28" })).toEqual([]);
  });

  it("explains the consequence of each supported override", () => {
    const warnings = parameterWarnings({
      ...INITIAL,
      evalue: 1e-10,
      max_target_seqs: 25,
      word_size: "11",
      max_matches_in_query_range: "2",
      outfmt: 7,
      short_query_adjust: false,
      match_score: "2",
      mismatch_score: "-3",
      gap_open: "5",
      gap_extend: "2",
      low_complexity_filter: false,
      mask_lookup_table_only: true,
      mask_lowercase: true,
      species_repeat_filter: true,
      additional_options: "-max_hsps 1",
    });

    expect(warnings.map((warning) => warning.key)).toEqual([
      "evalue",
      "outfmt",
      "gap_open",
      "species_repeat_filter",
      "additional_options",
    ]);
    expect(warnings[0]?.message).toContain("default 0.05");
    expect(warnings[0]?.message).toContain("which hits appear");
    expect(warnings).toHaveLength(5);
  });
});