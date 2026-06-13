import type { BlastProgram } from "@/api/endpoints";

export const PROGRAMS: {
  value: BlastProgram;
  label: string;
  desc: string;
  longDesc: string;
  dbType: "nucl" | "prot";
  defaultWordSize: number;
}[] = [
  {
    value: "blastn",
    label: "blastn",
    desc: "Nucleotide → Nucleotide",
    longDesc: "Search nucleotide databases using a nucleotide query.",
    dbType: "nucl",
    defaultWordSize: 28,
  },
  {
    value: "blastp",
    label: "blastp",
    desc: "Protein → Protein",
    longDesc: "Search protein databases using a protein query.",
    dbType: "prot",
    defaultWordSize: 6,
  },
  {
    value: "blastx",
    label: "blastx",
    desc: "Translated Nucleotide → Protein",
    longDesc: "Search protein databases using a translated nucleotide query.",
    dbType: "prot",
    defaultWordSize: 6,
  },
  {
    value: "tblastn",
    label: "tblastn",
    desc: "Protein → Translated Nucleotide",
    longDesc: "Search translated nucleotide databases using a protein query.",
    dbType: "nucl",
    defaultWordSize: 6,
  },
  {
    value: "tblastx",
    label: "tblastx",
    desc: "Translated Nucl. → Translated Nucl.",
    longDesc: "Search translated nucleotide databases using a translated nucleotide query.",
    dbType: "nucl",
    defaultWordSize: 3,
  },
];

export const BLASTN_OPTIMIZE: {
  value: string;
  label: string;
  desc: string;
  wordSize: number;
  evalue: number;
}[] = [
  {
    value: "megablast",
    label: "Highly similar sequences (megablast)",
    desc: "Best for intra-species comparisons",
    wordSize: 28,
    evalue: 0.05,
  },
  {
    value: "dc-megablast",
    label: "More dissimilar sequences (discontiguous megablast)",
    desc: "Best for cross-species searches",
    wordSize: 11,
    evalue: 0.05,
  },
  {
    value: "blastn",
    label: "Somewhat similar sequences (blastn)",
    desc: "Best for inter-species comparisons",
    wordSize: 7,
    evalue: 0.05,
  },
];

export const PRESETS: {
  label: string;
  desc: string;
  evalue: number;
  max_target_seqs: number;
}[] = [
  { label: "Quick scan", desc: "Fast, fewer results", evalue: 10, max_target_seqs: 50 },
  { label: "Standard", desc: "Balanced (default)", evalue: 0.05, max_target_seqs: 100 },
  {
    label: "Thorough",
    desc: "Low E-value, more targets",
    evalue: 1e-5,
    max_target_seqs: 500,
  },
  {
    label: "Publication",
    desc: "Stringent parameters",
    evalue: 1e-10,
    max_target_seqs: 1000,
  },
];

export const DB_DESCRIPTIONS: Record<
  string,
  { label: string; size: string; type: "nucl" | "prot" }
> = {
  core_nt: { label: "Core Nucleotide", size: "~250 GB", type: "nucl" },
  nt: { label: "Nucleotide collection", size: "~400 GB", type: "nucl" },
  nr: { label: "Non-redundant protein", size: "~300 GB", type: "prot" },
  swissprot: { label: "SwissProt", size: "~300 MB", type: "prot" },
  pdbnt: { label: "PDB nucleotide", size: "~200 MB", type: "nucl" },
  refseq_protein: { label: "RefSeq protein", size: "~100 GB", type: "prot" },
  "16S_ribosomal_RNA": { label: "16S ribosomal RNA", size: "~18 MB", type: "nucl" },
  ITS_RefSeq_Fungi: { label: "ITS RefSeq Fungi", size: "~8 MB", type: "nucl" },
};

export const DEFAULT_DATABASE_NAME = "core_nt";
export const DEFAULT_DATABASE_PATH = `blast-db/${DEFAULT_DATABASE_NAME}/${DEFAULT_DATABASE_NAME}`;

export const EXAMPLE_FASTA = `>example_16S_rRNA Escherichia coli 16S ribosomal RNA partial sequence
AGAGTTTGATCCTGGCTCAGATTGAACGCTGGCGGCAGGCCTAACACATGCAAGTCGAAC
GGTAACAGGAAGAAGCTTGCTTCTTTGCTGACGAGTGGCGGACGGGTGAGTAATGTCTG
GGAAACTGCCTGATGGAGGGGGATAACTACTGGAAACGGTAGCTAATACCGCATAACGTCG
CAAGACCAAAGAGGGGGACCTTAGGGCCTCTTGCCATCGGATGTGCCCAGATGGGATTAGC
TAGTAGGTGGGGTAACGGCTCACCTAGGCGACGATCCCTAGCTGGTCTGAGAGGATGACC
AGCCACACTGGAACTGAGACACGGTCCAGACTCCTACGGGAGGCAGCAGTGGGGAATATTG
CACAATGGGCGCAAGCCTGATGCAGCCATGCCGCGTGTATGAAGAAGGCCTTCGGGTTGT
AAAGTACTTTCAGCGGGGAGGAAGGGAGTAAAGTTAATACCTTTGCTCATTGA`;

export interface FormState {
  program: BlastProgram;
  db: string;
  query_data: string;
  /** NCBI nuccore accession. When set and `query_data` is empty, the backend resolves it via E-utilities at submit time. */
  query_accession: string;
  query_from: string;
  query_to: string;
  job_title: string;
  evalue: number;
  max_target_seqs: number;
  outfmt: number;
  /**
   * When true, request subject taxonomy columns (taxid + scientific name) in a
   * tabular result. Emits the verified canonical `-outfmt 7 std staxids
   * sscinames` specifier via `additional_options` and suppresses the integer
   * `outfmt` field so the submit never carries a double `-outfmt` flag (the
   * shard merge reads the full specifier from the single flag). See issue #29.
   */
  outfmt_taxonomy_columns: boolean;
  word_size: string;
  gap_open: string;
  gap_extend: string;
  match_score: string;
  mismatch_score: string;
  low_complexity_filter: boolean;
  short_query_adjust: boolean;
  max_matches_in_query_range: string;
  mask_lookup_table_only: boolean;
  mask_lowercase: boolean;
  species_repeat_filter: boolean;
  repeat_filter_taxid: string;
  additional_options: string;
  taxid: string;
  taxid_label: string;
  taxid_rank: string;
  is_inclusive: boolean;
  selectedCluster: string;
  optimize: string;
  enable_warmup: boolean;
  sharding_mode: "off" | "approximate" | "precise";
  db_auto_partition: boolean;
  /** Opt-out of automatic shard layout selection for warmed DBs. Default false. */
  disable_sharding: boolean;
}

export const INITIAL: FormState = {
  program: "blastn",
  db: DEFAULT_DATABASE_PATH,
  query_data: "",
  query_accession: "",
  query_from: "",
  query_to: "",
  job_title: "",
  evalue: 0.05,
  max_target_seqs: 100,
  outfmt: 5,
  outfmt_taxonomy_columns: false,
  word_size: "",
  gap_open: "",
  gap_extend: "",
  match_score: "",
  mismatch_score: "",
  low_complexity_filter: true,
  short_query_adjust: true,
  max_matches_in_query_range: "0",
  mask_lookup_table_only: false,
  mask_lowercase: false,
  species_repeat_filter: false,
  repeat_filter_taxid: "9606",
  additional_options: "",
  taxid: "",
  taxid_label: "",
  taxid_rank: "",
  is_inclusive: true,
  selectedCluster: "",
  optimize: "megablast",
  enable_warmup: false,
  sharding_mode: "off",
  db_auto_partition: false,
  disable_sharding: false,
};

export function formatJobTitleTimestamp(date: Date = new Date()): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}`;
}

export function buildGeneratedJobTitle(baseTitle: string, date: Date = new Date()): string {
  const trimmed = baseTitle.trim();
  const prefix = formatJobTitleTimestamp(date);
  return trimmed ? `${prefix} ${trimmed}` : prefix;
}

export function parsePositiveTaxid(value: string): number | null {
  const trimmed = value.trim();
  if (!/^[1-9]\d*$/.test(trimmed)) return null;
  const parsed = Number.parseInt(trimmed, 10);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

export function hasStructuredTaxidOptionConflict(additionalOptions: string): boolean {
  return /(?:^|\s)-(?:negative_)?taxids(?:\s|=|$)/.test(additionalOptions);
}

export function longestQuerySequenceLength(queryData: string): number | null {
  const trimmed = queryData.trim();
  if (!trimmed) return null;
  if (!trimmed.startsWith(">")) {
    const compact = trimmed.replace(/\s+/g, "");
    return compact.length > 0 ? compact.length : null;
  }
  let longest = 0;
  let current = "";
  for (const rawLine of trimmed.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    if (line.startsWith(">")) {
      longest = Math.max(longest, current.length);
      current = "";
      continue;
    }
    current += line.replace(/\s+/g, "");
  }
  longest = Math.max(longest, current.length);
  return longest > 0 ? longest : null;
}

export function shouldUseBlastnShortTask(form: FormState): boolean {
  if (form.program !== "blastn" || !form.short_query_adjust) return false;
  const longest = longestQuerySequenceLength(form.query_data);
  return longest !== null && longest <= 50;
}

export function hasCliFlag(options: string, flag: string): boolean {
  return new RegExp(`(?:^|\\s)${flag.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?:\\s|=|$)`).test(options);
}

export function buildCommandString(
  form: FormState,
  programMeta: (typeof PROGRAMS)[0],
  options: { effectiveSearchSpace?: number } = {},
): string {
  const dbName = form.db.split("/").pop() || form.db;
  const parts = [form.program, "-db", dbName, "-evalue", String(form.evalue)];
  const useShortTask = shouldUseBlastnShortTask(form);
  if (useShortTask) parts.push("-task", "blastn-short");
  if (form.word_size) parts.push("-word_size", form.word_size);
  else if (!useShortTask) parts.push("-word_size", String(programMeta.defaultWordSize));
  parts.push("-max_target_seqs", String(form.max_target_seqs));
  parts.push("-outfmt", String(form.outfmt));
  if (form.gap_open) parts.push("-gapopen", form.gap_open);
  if (form.gap_extend) parts.push("-gapextend", form.gap_extend);
  if (form.match_score && form.program === "blastn") parts.push("-reward", form.match_score);
  if (form.mismatch_score && form.program === "blastn") {
    parts.push("-penalty", form.mismatch_score);
  }
  if (form.low_complexity_filter && form.program === "blastn") parts.push("-dust", "yes");
  if (form.max_matches_in_query_range && form.max_matches_in_query_range !== "0") {
    parts.push("-culling_limit", form.max_matches_in_query_range);
  }
  if (form.mask_lookup_table_only) parts.push("-soft_masking", "true");
  else if (
    form.low_complexity_filter &&
    form.program === "blastn" &&
    !hasCliFlag(form.additional_options || "", "-soft_masking")
  ) {
    parts.push("-soft_masking", "false");
  }
  if (form.mask_lowercase) parts.push("-lcase_masking");
  if (form.species_repeat_filter && form.repeat_filter_taxid.trim()) {
    parts.push("-window_masker_taxid", form.repeat_filter_taxid.trim());
  }
  if (form.query_from && form.query_to) {
    parts.push("-query_loc", `${form.query_from}-${form.query_to}`);
  }
  const taxid = parsePositiveTaxid(form.taxid);
  if (taxid) {
    parts.push(form.is_inclusive ? "-taxids" : "-negative_taxids", String(taxid));
  }
  if (
    options.effectiveSearchSpace &&
    Number.isFinite(options.effectiveSearchSpace) &&
    options.effectiveSearchSpace > 0 &&
    !hasCliFlag(form.additional_options || "", "-searchsp")
  ) {
    parts.push("-searchsp", String(Math.floor(options.effectiveSearchSpace)));
  }
  if (form.additional_options?.trim()) parts.push(form.additional_options.trim());
  parts.push("-query", "query.fasta", "-out", "results.out");
  return parts.join(" ");
}