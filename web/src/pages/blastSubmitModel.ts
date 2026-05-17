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
  query_from: string;
  query_to: string;
  job_title: string;
  evalue: number;
  max_target_seqs: number;
  outfmt: number;
  word_size: string;
  gap_open: string;
  gap_extend: string;
  match_score: string;
  mismatch_score: string;
  low_complexity_filter: boolean;
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
  db: "",
  query_data: "",
  query_from: "",
  query_to: "",
  job_title: "",
  evalue: 0.05,
  max_target_seqs: 100,
  outfmt: 5,
  word_size: "",
  gap_open: "",
  gap_extend: "",
  match_score: "",
  mismatch_score: "",
  low_complexity_filter: true,
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

export function parsePositiveTaxid(value: string): number | null {
  const trimmed = value.trim();
  if (!/^[1-9]\d*$/.test(trimmed)) return null;
  const parsed = Number.parseInt(trimmed, 10);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

export function hasStructuredTaxidOptionConflict(additionalOptions: string): boolean {
  return /(?:^|\s)-(?:negative_)?taxids(?:\s|=|$)/.test(additionalOptions);
}

export function buildCommandString(form: FormState, programMeta: (typeof PROGRAMS)[0]): string {
  const dbName = form.db.split("/").pop() || form.db;
  const parts = [form.program, "-db", dbName, "-evalue", String(form.evalue)];
  if (form.word_size) parts.push("-word_size", form.word_size);
  else parts.push("-word_size", String(programMeta.defaultWordSize));
  parts.push("-max_target_seqs", String(form.max_target_seqs));
  parts.push("-outfmt", String(form.outfmt));
  if (form.gap_open) parts.push("-gapopen", form.gap_open);
  if (form.gap_extend) parts.push("-gapextend", form.gap_extend);
  if (form.match_score && form.program === "blastn") parts.push("-reward", form.match_score);
  if (form.mismatch_score && form.program === "blastn") {
    parts.push("-penalty", form.mismatch_score);
  }
  if (form.low_complexity_filter && form.program === "blastn") parts.push("-dust", "yes");
  if (form.query_from && form.query_to) {
    parts.push("-query_loc", `${form.query_from}-${form.query_to}`);
  }
  const taxid = parsePositiveTaxid(form.taxid);
  if (taxid) {
    parts.push(form.is_inclusive ? "-taxids" : "-negative_taxids", String(taxid));
  }
  if (form.additional_options?.trim()) parts.push(form.additional_options.trim());
  parts.push("-query", "query.fasta", "-out", "results.out");
  return parts.join(" ");
}