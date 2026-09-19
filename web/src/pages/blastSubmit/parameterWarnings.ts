import {
  BLASTN_OPTIMIZE,
  INITIAL,
  PROGRAMS,
  type FormState,
} from "@/pages/blastSubmitModel";

export interface ParameterWarning {
  key: keyof FormState;
  message: string;
}

export function parameterWarnings(form: FormState): ParameterWarning[] {
  const warnings: ParameterWarning[] = [];
  const searchChanges: string[] = [];
  const recommendedWordSize = form.program === "blastn"
    ? BLASTN_OPTIMIZE.find((option) => option.value === form.optimize)?.wordSize
    : PROGRAMS.find((program) => program.value === form.program)?.defaultWordSize;

  if (form.evalue !== INITIAL.evalue) {
    searchChanges.push(`E-value ${form.evalue} (default ${INITIAL.evalue})`);
  }
  if (form.max_target_seqs !== INITIAL.max_target_seqs) {
    searchChanges.push(`hit limit ${form.max_target_seqs} (default ${INITIAL.max_target_seqs})`);
  }
  if (
    form.word_size !== "" &&
    form.word_size !== String(recommendedWordSize ?? "")
  ) {
    searchChanges.push(`word size ${form.word_size}`);
  }
  if (form.max_matches_in_query_range !== INITIAL.max_matches_in_query_range) {
    searchChanges.push(`culling limit ${form.max_matches_in_query_range}`);
  }
  if (form.program === "blastn" && form.short_query_adjust !== INITIAL.short_query_adjust) {
    searchChanges.push("short-query adjustment off");
  }
  if (searchChanges.length > 0) {
    warnings.push({
      key: "evalue",
      message: `Custom search settings: ${searchChanges.join(", ")}. These can change which hits appear, sensitivity, and runtime.`,
    });
  }
  if (
    form.outfmt !== INITIAL.outfmt ||
    form.outfmt_taxonomy_columns !== INITIAL.outfmt_taxonomy_columns
  ) {
    warnings.push({
      key: "outfmt",
      message: "Custom output format changes downstream parsing and reformatting options.",
    });
  }
  if (
    form.match_score !== "" ||
    form.mismatch_score !== "" ||
    form.gap_open !== "" ||
    form.gap_extend !== ""
  ) {
    warnings.push({
      key: "gap_open",
      message: "Custom scoring or gap costs can change alignment boundaries, scores, and hit ranking.",
    });
  }
  if (
    form.low_complexity_filter !== INITIAL.low_complexity_filter ||
    form.mask_lookup_table_only !== INITIAL.mask_lookup_table_only ||
    form.mask_lowercase !== INITIAL.mask_lowercase ||
    form.species_repeat_filter !== INITIAL.species_repeat_filter
  ) {
    warnings.push({
      key: "species_repeat_filter",
      message: "Custom masking or filtering can suppress expected hits or expose repetitive and compositionally biased matches.",
    });
  }
  if (form.additional_options.trim()) {
    warnings.push({
      key: "additional_options",
      message: "Additional BLAST flags override the recommended form defaults and can change result comparability.",
    });
  }

  return warnings;
}