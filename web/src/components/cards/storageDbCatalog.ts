/**
 * Marks a catalog entry that NCBI does NOT publish in the current S3 mirror
 * (`ncbi-blast-databases` bucket) and that cannot be `elastic-blast`-pulled
 * in the BLAST+ 2.17 (v5) pipeline. The dashboard surfaces a clear badge
 * pointing at the real source instead of showing the generic
 * "Not in current NCBI snapshot" warning and a Download button that will
 * 404. Verified 2026-05-23 against `latest-dir` `2026-05-09-01-05-02`.
 *
 * - `no-prebuilt` — NCBI ships no pre-built BLAST DB anywhere (S3 nor
 *   FTP/blast/db); only raw GenBank / repository files. User must run
 *   `makeblastdb` locally on the FASTA.
 * - `v4-only` — Removed from BLAST v5; only legacy v4 tarballs remain in
 *   `/blast/db/v4/`. Not consumable by the elastic-blast 2.17 pipeline
 *   without a v4→v5 conversion step.
 * - `too-large` — NCBI does not bulk-distribute (e.g. WGS, SRA); use
 *   Entrez / online BLAST / the SRA Toolkit instead.
 */
export interface BlastDbUnsupported {
  reason: "no-prebuilt" | "v4-only" | "too-large";
  hint: string;
  sourceUrl: string;
}

export interface BlastDbCatalogItem {
  value: string;
  label: string;
  desc: string;
  size: string;
  estFiles: number;
  estMinutes: string;
  category: "Small / Test" | "Medium" | "Large";
  type: "nucl" | "prot";
  /** When set, this DB is not consumable by elastic-blast — see type doc. */
  unsupported?: BlastDbUnsupported;
}

export const DB_CATALOG: BlastDbCatalogItem[] = [
  {
    value: "16S_ribosomal_RNA",
    label: "16S ribosomal RNA",
    desc: "Prokaryotic small subunit rRNA — ideal for microbial ID and metagenomics.",
    size: "~18 MB",
    estFiles: 12,
    estMinutes: "< 1 min",
    category: "Small / Test",
    type: "nucl",
  },
  {
    value: "18S_fungal_sequences",
    label: "18S fungal sequences",
    desc: "Fungal small subunit rRNA for fungal taxonomy.",
    size: "~3 MB",
    estFiles: 10,
    estMinutes: "< 1 min",
    category: "Small / Test",
    type: "nucl",
  },
  {
    value: "ITS_RefSeq_Fungi",
    label: "ITS RefSeq Fungi",
    desc: "Internal Transcribed Spacer regions for fungal species-level ID.",
    size: "~8 MB",
    estFiles: 10,
    estMinutes: "< 1 min",
    category: "Small / Test",
    type: "nucl",
  },
  {
    value: "pdbnt",
    label: "PDB nucleotide",
    desc: "Nucleotide sequences from the Protein Data Bank (3D structures).",
    size: "~200 MB",
    estFiles: 15,
    estMinutes: "~2 min",
    category: "Medium",
    type: "nucl",
  },
  {
    value: "swissprot",
    label: "SwissProt",
    desc: "Curated, high-quality protein sequences from UniProt/Swiss-Prot.",
    size: "~300 MB",
    estFiles: 15,
    estMinutes: "~3 min",
    category: "Medium",
    type: "prot",
  },
  {
    value: "core_nt",
    label: "Core nucleotide",
    desc: "Curated subset of nt — major organisms, smaller and faster than full nt.",
    size: "~250 GB",
    estFiles: 600,
    estMinutes: "~2-4 hours",
    category: "Large",
    type: "nucl",
  },
  {
    value: "nt",
    label: "Nucleotide collection",
    desc: "All GenBank + RefSeq nucleotide sequences. Comprehensive but very large.",
    size: "~400 GB",
    estFiles: 900,
    estMinutes: "~4-8 hours",
    category: "Large",
    type: "nucl",
  },
  {
    value: "nr",
    label: "Non-redundant protein",
    desc: "All non-redundant GenBank protein translations + RefSeq + PDB + SwissProt.",
    size: "~300 GB",
    estFiles: 700,
    estMinutes: "~3-6 hours",
    category: "Large",
    type: "prot",
  },
  {
    value: "refseq_protein",
    label: "RefSeq protein",
    desc: "NCBI Reference Sequence protein database — curated and non-redundant.",
    size: "~100 GB",
    estFiles: 300,
    estMinutes: "~1-3 hours",
    category: "Large",
    type: "prot",
  },
  // -- Extended NCBI standard nucleotide catalogue --------------------------
  // Surfaced in the BLAST submit "Standard databases" tab so the operator
  // can see (and request) the same set NCBI Web BLAST exposes, even when the
  // database has not yet been pulled into our blast-db storage container.
  // Sizes are NCBI's published 2026 figures rounded to one significant digit
  // and are deliberately conservative — they only drive the download UX.
  {
    value: "refseq_select_rna",
    label: "RefSeq Select RNA sequences",
    desc: "A subset of RefSeq RNA representing one transcript per protein-coding locus.",
    size: "~2 GB",
    estFiles: 25,
    estMinutes: "~10 min",
    category: "Medium",
    type: "nucl",
  },
  {
    value: "refseq_rna",
    label: "Reference RNA sequences",
    desc: "Curated NCBI RefSeq RNA — transcripts across multiple organisms.",
    size: "~25 GB",
    estFiles: 80,
    estMinutes: "~30-60 min",
    category: "Large",
    type: "nucl",
  },
  {
    value: "refseq_reference_genomes",
    label: "RefSeq Reference genomes",
    desc: "Reference-quality assembled genomes selected by RefSeq.",
    size: "~30 GB",
    estFiles: 60,
    estMinutes: "~30-60 min",
    category: "Large",
    type: "nucl",
    unsupported: {
      reason: "no-prebuilt",
      hint: "NCBI does not publish a pre-built BLAST DB under this name. Use refseq_select / refseq_rna, or build from RefSeq genome assemblies with makeblastdb.",
      sourceUrl: "https://ftp.ncbi.nlm.nih.gov/refseq/release/",
    },
  },
  {
    value: "refseq_genomes",
    label: "RefSeq Genome Database",
    desc: "All NCBI RefSeq assembled genomes — broader than reference_genomes.",
    size: "~600 GB",
    estFiles: 1500,
    estMinutes: "~6-12 hours",
    category: "Large",
    type: "nucl",
    unsupported: {
      reason: "no-prebuilt",
      hint: "Not published as a monolithic BLAST DB. Pull per-organism RefSeq genome assemblies and build with makeblastdb.",
      sourceUrl: "https://ftp.ncbi.nlm.nih.gov/genomes/refseq/",
    },
  },
  {
    value: "wgs",
    label: "Whole-genome shotgun contigs",
    desc: "GenBank whole-genome shotgun contigs — very large, taxonomy-broad.",
    size: "~3 TB",
    estFiles: 3000,
    estMinutes: ">12 hours",
    category: "Large",
    type: "nucl",
    unsupported: {
      reason: "too-large",
      hint: "NCBI does not bulk-distribute WGS as a single BLAST DB. Use Entrez / NCBI WGS BLAST online, or fetch a per-project WGS tarball and build with makeblastdb.",
      sourceUrl: "https://www.ncbi.nlm.nih.gov/Traces/wgs/",
    },
  },
  {
    value: "est",
    label: "Expressed sequence tags (EST)",
    desc: "GenBank single-pass cDNA reads from many organisms.",
    size: "~100 GB",
    estFiles: 300,
    estMinutes: "~2-4 hours",
    category: "Large",
    type: "nucl",
    unsupported: {
      reason: "v4-only",
      hint: "Removed from BLAST v5; only legacy v4 tarballs remain. Not consumable by elastic-blast 2.17 (v5).",
      sourceUrl: "https://ftp.ncbi.nlm.nih.gov/blast/db/v4/",
    },
  },
  {
    value: "sra",
    label: "Sequence Read Archive (SRA)",
    desc: "Subset of SRA exposed to BLAST — very large, taxonomy-broad.",
    size: ">5 TB",
    estFiles: 5000,
    estMinutes: ">24 hours",
    category: "Large",
    type: "nucl",
    unsupported: {
      reason: "too-large",
      hint: "SRA is not distributed as a BLAST DB. Use the SRA Toolkit (prefetch/fasterq-dump) and BLAST per-run, or use NCBI's online SRA BLAST.",
      sourceUrl: "https://github.com/ncbi/sra-tools",
    },
  },
  {
    value: "tsa_nt",
    label: "Transcriptome Shotgun Assembly (TSA, nucleotide)",
    desc: "Assembled transcriptome contigs deposited in GenBank/TSA (nucleotide).",
    size: "~200 GB",
    estFiles: 400,
    estMinutes: "~3-6 hours",
    category: "Large",
    type: "nucl",
  },
  {
    value: "tls",
    label: "Targeted Loci (TLS)",
    desc: "GenBank Targeted Locus Study sequences (often metagenomic markers).",
    size: "~5 GB",
    estFiles: 30,
    estMinutes: "~15 min",
    category: "Medium",
    type: "nucl",
    unsupported: {
      reason: "no-prebuilt",
      hint: "NCBI does not publish a pre-built BLAST DB for TLS. Download the raw GenBank flat files and build with makeblastdb -dbtype nucl.",
      sourceUrl: "https://ftp.ncbi.nlm.nih.gov/genbank/tls/",
    },
  },
  {
    value: "htgs",
    label: "High-throughput genomic sequences (HTGS)",
    desc: "High-throughput genome sequences (unfinished/working-draft).",
    size: "~50 GB",
    estFiles: 120,
    estMinutes: "~1-2 hours",
    category: "Large",
    type: "nucl",
    unsupported: {
      reason: "v4-only",
      hint: "Removed from BLAST v5; only legacy v4 tarballs remain (htgs_v4.*.tar.gz). Not consumable by elastic-blast 2.17 (v5).",
      sourceUrl: "https://ftp.ncbi.nlm.nih.gov/blast/db/v4/",
    },
  },
  {
    value: "patnt",
    label: "Patent sequences (nucleotide)",
    desc: "Patent-derived nucleotide sequences from GenBank.",
    size: "~40 GB",
    estFiles: 90,
    estMinutes: "~1 hour",
    category: "Large",
    type: "nucl",
  },
  {
    value: "RefSeq_Gene",
    label: "Human RefSeqGene sequences",
    desc: "Human gene reference standard records for clinical/diagnostic use.",
    size: "~1 GB",
    estFiles: 20,
    estMinutes: "~5 min",
    category: "Small / Test",
    type: "nucl",
    unsupported: {
      reason: "v4-only",
      hint: "Removed from BLAST v5; only refseqgene_v4.tar.gz remains. Not consumable by elastic-blast 2.17 (v5).",
      sourceUrl: "https://ftp.ncbi.nlm.nih.gov/blast/db/v4/",
    },
  },
  {
    value: "gss",
    label: "Genomic survey sequences (GSS)",
    desc: "Single-pass genomic survey reads from GenBank GSS division.",
    size: "~30 GB",
    estFiles: 100,
    estMinutes: "~1 hour",
    category: "Large",
    type: "nucl",
    unsupported: {
      reason: "v4-only",
      hint: "Removed from BLAST v5; only legacy v4 tarballs remain (gss_v4.*.tar.gz). Not consumable by elastic-blast 2.17 (v5).",
      sourceUrl: "https://ftp.ncbi.nlm.nih.gov/blast/db/v4/",
    },
  },
  {
    value: "dbsts",
    label: "Sequence tagged sites (dbSTS)",
    desc: "Short, uniquely amplifiable genomic landmarks (PCR markers).",
    size: "~5 GB",
    estFiles: 25,
    estMinutes: "~15 min",
    category: "Medium",
    type: "nucl",
    unsupported: {
      reason: "no-prebuilt",
      hint: "dbSTS is retired by NCBI and has no pre-built BLAST DB. Download UniSTS / Daily.FASTA and build with makeblastdb -dbtype nucl.",
      sourceUrl: "https://ftp.ncbi.nlm.nih.gov/repository/dbSTS/",
    },
  },
  {
    value: "pdb",
    label: "PDB protein",
    desc: "Protein sequences associated with PDB 3D structures.",
    size: "~120 MB",
    estFiles: 10,
    estMinutes: "< 2 min",
    category: "Small / Test",
    type: "prot",
  },
  {
    value: "28S_fungal_sequences",
    label: "28S fungal sequences (LSU)",
    desc: "Fungal large subunit rRNA (LSU) reference sequences.",
    size: "~5 MB",
    estFiles: 10,
    estMinutes: "< 1 min",
    category: "Small / Test",
    type: "nucl",
  },
];

export function formatNcbiVersion(v: string | null | undefined): string {
  if (!v) return "";
  const match = v.match(/^(\d{4}-\d{2}-\d{2})-(\d{2})-(\d{2})-(\d{2})$/);
  if (!match) return v;
  return `${match[1]} ${match[2]}:${match[3]}:${match[4]}`;
}

const NCBI_BLAST_DB_V5_FTP = "https://ftp.ncbi.nlm.nih.gov/blast/db/v5/";

export function ncbiBlastDbFtpUrl(
  dbName: string | null | undefined,
  dbType: "nucl" | "prot" | null | undefined,
): string {
  if (!dbName || !dbType) return NCBI_BLAST_DB_V5_FTP;
  if (!/^[A-Za-z0-9_.-]+$/.test(dbName)) return NCBI_BLAST_DB_V5_FTP;
  return `${NCBI_BLAST_DB_V5_FTP}${encodeURIComponent(dbName)}-${dbType}-metadata.json`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

export function formatStorageDate(iso: string | null | undefined): string {
  if (!iso) return "";
  try {
    const date = new Date(iso);
    const pad = (value: number) => value.toString().padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  } catch {
    return "";
  }
}