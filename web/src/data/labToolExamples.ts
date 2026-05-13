// Curated examples for Lab Tools and Custom Database Builder.
// Sequences are real reference fragments shortened to fit inline use; accession
// IDs come from public NCBI records. Designed so each example is self-contained
// and can be applied with a single click.

export interface ExamplePreset<TValues> {
  id: string;
  label: string;
  description: string;
  recommended?: boolean;
  values: TValues;
}

// ── Cost Estimator ────────────────────────────────────────────
export interface CostExampleValues {
  sku: string;
  nodes: number;
  hours: number;
  pdSize: number;
  dbSize: number;
}

export const COST_EXAMPLES: ExamplePreset<CostExampleValues>[] = [
  {
    id: "quick-test",
    label: "Quick test",
    description: "1 small node, 30 min, tiny DB — sanity check",
    values: { sku: "Standard_D4s_v5", nodes: 1, hours: 0.5, pdSize: 200, dbSize: 5 },
  },
  {
    id: "production-survey",
    label: "Production survey",
    description: "3 × E16s_v5, 2h, 1 TB PD, 50 GB DB (current default)",
    recommended: true,
    values: { sku: "Standard_E16s_v5", nodes: 3, hours: 2, pdSize: 1000, dbSize: 50 },
  },
  {
    id: "whole-genome-large",
    label: "Whole-genome large",
    description: "8 × E32s_v5, 8h, 4 TB PD, 400 GB DB (nt/nr scale)",
    values: { sku: "Standard_E32s_v5", nodes: 8, hours: 8, pdSize: 4000, dbSize: 400 },
  },
];

// ── Preprocessor ──────────────────────────────────────────────
export interface PreprocessExampleValues {
  inputData: string;
  format: "auto" | "fasta" | "fastq";
  minLength: number;
  minQuality: number;
}

export const PREPROCESS_EXAMPLES: ExamplePreset<PreprocessExampleValues>[] = [
  {
    id: "small-fasta",
    label: "Small FASTA (5 seqs)",
    description: "Clean 16S V3 fragments — see baseline statistics",
    values: {
      inputData: [
        ">seq_1 16S V3 (E. coli)",
        "CCTACGGGAGGCAGCAGTGGGGAATATTGCACAATGGGCGCAAGCCTGATGCAGCC",
        ">seq_2 16S V3 (B. subtilis)",
        "CCTACGGGAGGCAGCAGTAGGGAATCTTCCGCAATGGACGAAAGTCTGACGGAGCA",
        ">seq_3 16S V3 (Lactobacillus)",
        "CCTACGGGAGGCAGCAGTAGGGAATCTTCCACAATGGACGCAAGTCTGATGGAGCA",
        ">seq_4 16S V3 (Pseudomonas)",
        "CCTACGGGAGGCAGCAGTGGGGAATATTGGACAATGGGCGAAAGCCTGATCCAGCC",
        ">seq_5 16S V3 (Staphylococcus)",
        "CCTACGGGAGGCAGCAGTAGGGAATCTTCCGCAATGGGCGAAAGCCTGACGGAGCA",
      ].join("\n"),
      format: "fasta",
      minLength: 0,
      minQuality: 0,
    },
  },
  {
    id: "fastq-quality",
    label: "FASTQ with quality",
    description: "8 reads, some short / low-Q — demonstrates filtering",
    recommended: true,
    values: {
      inputData: [
        "@read_1",
        "ACGTACGTACGTACGTACGTACGTACGTAC",
        "+",
        "IIIIIIIIIIIIIIIIIIIIIIIIIIIIII",
        "@read_2_short",
        "ACGTACGTAC",
        "+",
        "IIIIIIIIII",
        "@read_3_lowq",
        "ACGTACGTACGTACGTACGTACGTACGTAC",
        "+",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "@read_4",
        "TGCATGCATGCATGCATGCATGCATGCATG",
        "+",
        "HHHHHHHHHHHHHHHHHHHHHHHHHHHHHH",
        "@read_5",
        "GATCGATCGATCGATCGATCGATCGATCGA",
        "+",
        "IIIIIIIIIIIIIIIIIIIIIIIIIIIIII",
        "@read_6_lowq",
        "GATCGATCGATCGATCGATCGATCGATCGA",
        "+",
        '""""""""""""""""""""""""""""""',
        "@read_7",
        "CGTAGCTAGCTAGCTAGCTAGCTAGCTAGC",
        "+",
        "HHHHHHHHHHHHHHHHHHHHHHHHHHHHHH",
        "@read_8_short",
        "CGTAGCT",
        "+",
        "HHHHHHH",
      ].join("\n"),
      format: "fastq",
      minLength: 25,
      minQuality: 20,
    },
  },
  {
    id: "messy-fasta",
    label: "Mixed messy input",
    description: "Blank lines, mixed case, short entries — shows normalization",
    values: {
      inputData: [
        "",
        ">seq_one mixed-case messy",
        "acgtACGTacgtACGTacgtACGTacgtACGT",
        "",
        ">seq_two",
        "gatcGATCgatcGATCgatcGATCgatcGATC",
        ">seq_three blank lines",
        "",
        "cgtaCGTAcgtaCGTAcgtaCGTA",
        ">seq_four short",
        "ACGT",
        ">seq_five",
        "TTTTAAAACCCCGGGGTTTTAAAACCCCGGGG",
      ].join("\n"),
      format: "fasta",
      minLength: 20,
      minQuality: 0,
    },
  },
];

// ── Primer Design ─────────────────────────────────────────────
export interface PrimerExampleValues {
  sequence: string;
  targetStart: number;
  targetLength: number;
  productMin: number;
  productMax: number;
}

export const PRIMER_EXAMPLES: ExamplePreset<PrimerExampleValues>[] = [
  {
    id: "16s-v3v4",
    label: "16S rRNA V3-V4",
    description: "E. coli 16S ~470 bp — standard microbial ID region",
    recommended: true,
    values: {
      sequence: [
        "AGAGTTTGATCCTGGCTCAGATTGAACGCTGGCGGCAGGCCTAACACATGCAAGTCGAAC",
        "GGTAACAGGAAGAAGCTTGCTTCTTTGCTGACGAGTGGCGGACGGGTGAGTAATGTCTG",
        "GGAAACTGCCTGATGGAGGGGGATAACTACTGGAAACGGTAGCTAATACCGCATAACGTCG",
        "CAAGACCAAAGAGGGGGACCTTAGGGCCTCTTGCCATCGGATGTGCCCAGATGGGATTAGC",
        "TAGTAGGTGGGGTAACGGCTCACCTAGGCGACGATCCCTAGCTGGTCTGAGAGGATGACCA",
        "GCCACACTGGAACTGAGACACGGTCCAGACTCCTACGGGAGGCAGCAGTGGGGAATATTGC",
        "ACAATGGGCGCAAGCCTGATGCAGCCATGCCGCGTGTATGAAGAAGGCCTTCGGGTTGTAA",
        "AGTACTTTCAGCGGGGAGGAAGGGAGTAAAGTTAATACCTTTGCTCATTGA",
      ].join(""),
      targetStart: 100,
      targetLength: 200,
      productMin: 200,
      productMax: 500,
    },
  },
  {
    id: "gapdh-cdna",
    label: "GAPDH cDNA",
    description: "Human GAPDH ~600 bp — qPCR housekeeping gene",
    values: {
      sequence: [
        "ATGGGGAAGGTGAAGGTCGGAGTCAACGGATTTGGTCGTATTGGGCGCCTGGTCACCAGG",
        "GCTGCTTTTAACTCTGGTAAAGTGGATATTGTTGCCATCAATGACCCCTTCATTGACCTCA",
        "ACTACATGGTTTACATGTTCCAATATGATTCCACCCATGGCAAATTCCATGGCACCGTCAAG",
        "GCTGAGAACGGGAAGCTTGTCATCAATGGAAATCCCATCACCATCTTCCAGGAGCGAGATCC",
        "CTCCAAAATCAAGTGGGGCGATGCTGGCGCTGAGTACGTCGTGGAGTCCACTGGCGTCTTCA",
        "CCACCATGGAGAAGGCTGGGGCTCATTTGCAGGGGGGAGCCAAAAGGGTCATCATCTCTGCC",
        "CCCTCTGCTGATGCCCCCATGTTCGTCATGGGTGTGAACCATGAGAAGTATGACAACAGCCT",
        "CAAGATCATCAGCAATGCCTCCTGCACCACCAACTGCTTAGCACCCCTGGCCAAGGTCATCC",
        "ATGACAACTTTGGTATCGTGGAAGGACTCATGACCACAGTCCATGCCATCACTGCCACCCAG",
        "AAGACTGTGGATGGCCCCTCCGGGAAACTGTGGCGTGATGGCCGC",
      ].join(""),
      targetStart: 150,
      targetLength: 200,
      productMin: 100,
      productMax: 300,
    },
  },
  {
    id: "sars2-n-gene",
    label: "SARS-CoV-2 N gene",
    description: "N gene fragment ~800 bp — diagnostic PCR target",
    values: {
      sequence: [
        "ATGTCTGATAATGGACCCCAAAATCAGCGAAATGCACCCCGCATTACGTTTGGTGGACCCTC",
        "AGATTCAACTGGCAGTAACCAGAATGGAGAACGCAGTGGGGCGCGATCAAAACAACGTCGG",
        "CCCCAAGGTTTACCCAATAATACTGCGTCTTGGTTCACCGCTCTCACTCAACATGGCAAGGA",
        "AGACCTTAAATTCCCTCGAGGACAAGGCGTTCCAATTAACACCAATAGCAGTCCAGATGACC",
        "AAATTGGCTACTACCGAAGAGCTACCAGACGAATTCGTGGTGGTGACGGTAAAATGAAAGAT",
        "CTCAGTCCAAGATGGTATTTCTACTACCTAGGAACTGGGCCAGAAGCTGGACTTCCCTATGG",
        "TGCTAACAAAGACGGCATCATATGGGTTGCAACTGAGGGAGCCTTGAATACACCAAAAGATC",
        "ACATTGGCACCCGCAATCCTGCTAACAATGCTGCAATCGTGCTACAACTTCCTCAAGGAACA",
        "ACATTGCCAAAAGGCTTCTACGCAGAAGGGAGCAGAGGCGGCAGTCAAGCCTCTTCTCGTTC",
        "CTCATCACGTAGTCGCAACAGTTCAAGAAATTCAACTCCAGGCAGCAGTAAACGAACTTCTC",
        "CTGCTAGAATGGCTGGCAATGGCGGTGATGCTGCTCTTGCTTTGCTGCTGCTTGACAGATT",
        "GAACCAGCTTGAGAGCAAAATGTCTGGTAAAGGT",
      ].join(""),
      targetStart: 200,
      targetLength: 300,
      productMin: 100,
      productMax: 250,
    },
  },
];

// ── Taxonomy ──────────────────────────────────────────────────
export interface TaxonomyExampleValues {
  accessions: string;
}

export const TAXONOMY_EXAMPLES: ExamplePreset<TaxonomyExampleValues>[] = [
  {
    id: "microbial-16s",
    label: "Microbial 16S set",
    description: "Common environmental 16S rRNA accessions",
    recommended: true,
    values: {
      accessions: "NR_074549.1 NR_113800.1 NR_117661.1 NR_044838.1",
    },
  },
  {
    id: "viral-set",
    label: "Viral set",
    description: "SARS-CoV-2 reference genomes and segments",
    values: {
      accessions: "NC_045512.2 MN908947.3 MT007544.1",
    },
  },
  {
    id: "mammalian-protein",
    label: "Mammalian protein set",
    description: "Human GAPDH, actin, and beta-globin proteins",
    values: {
      accessions: "NP_001092.1 NP_001605.1 NP_000509.1",
    },
  },
];

// ── Custom Database Builder ───────────────────────────────────
export interface CustomDbExampleValues {
  dbName: string;
  dbType: "nucl" | "prot";
  title: string;
  fastaData: string;
}

export const CUSTOM_DB_EXAMPLES: ExamplePreset<CustomDbExampleValues>[] = [
  {
    id: "small-nucl",
    label: "Small nucleotide (5 seqs)",
    description: "Toy DNA database for testing makeblastdb",
    values: {
      dbName: "demo_nucl_db",
      dbType: "nucl",
      title: "Demo nucleotide database",
      fastaData: [
        ">my_seq_1 Example nucleotide sequence",
        "ATGCGATCGATCGATCGATCGATCGATCGATCGATCGATCG",
        "ATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGA",
        ">my_seq_2 Another sequence",
        "GCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAG",
        "CTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGC",
        ">my_seq_3 Third sequence",
        "TTTTAAAACCCCGGGGTTTTAAAACCCCGGGGTTTTAAAA",
        ">my_seq_4 Fourth sequence",
        "ACACACACACACACACACACACACACACACACACACACAC",
        ">my_seq_5 Fifth sequence",
        "GATCGATCGATCGATCGATCGATCGATCGATCGATCGATC",
      ].join("\n"),
    },
  },
  {
    id: "small-prot",
    label: "Small protein (5 seqs)",
    description: "Toy protein database — hemoglobin-like fragments",
    recommended: true,
    values: {
      dbName: "demo_prot_db",
      dbType: "prot",
      title: "Demo protein database",
      fastaData: [
        ">protein_1 Hemoglobin alpha fragment",
        "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTK",
        "TYFPHFDLSHGSAQVKGHGKKVADALTNAVAHVDDMPNALS",
        ">protein_2 Hemoglobin beta fragment",
        "MGLSDGEWQLVLNVWGKVEADIPGHGQEVLIRLFKGHPETL",
        ">protein_3 Myoglobin fragment",
        "MVLSEGEWQLVLHVWAKVEADVAGHGQDILIRLFKSHPETLE",
        ">protein_4 Cytochrome c fragment",
        "MGDVEKGKKIFIMKCSQCHTVEKGGKHKTGPNLHGLFGRKTG",
        ">protein_5 Insulin fragment",
        "MALWMRLLPLLALLALWGPDPAAAFVNQHLCGSHLVEALYLV",
      ].join("\n"),
    },
  },
  {
    id: "barcoding-coi",
    label: "Barcoding COI markers",
    description: "COI barcode fragments for species ID (metabarcoding demo)",
    values: {
      dbName: "barcoding_coi",
      dbType: "nucl",
      title: "COI DNA barcoding markers",
      fastaData: [
        ">COI_Drosophila_melanogaster",
        "AACTTTATATTTTATTTTTGGAGCTTGAGCAGGAATAGTGGG",
        "AACTTCTTTATTAATTTTACTGCTTTAAGAAGTTTATTAA",
        ">COI_Homo_sapiens",
        "TTCATAATCGGAGCCCCTGATATAGCATTTCCTCGAATAAAC",
        "AACATAAGCTTTTGACTACTTCCCCCATCATTCCTCCTCCTC",
        ">COI_Mus_musculus",
        "CCTCCTATTATTATCACTCCCTGTCCTCTCAGGATTTGTTTC",
        "CATCACATCACCGCACTTACTACTACTATCCCTTCCCG",
      ].join("\n"),
    },
  },
];
