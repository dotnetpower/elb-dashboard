export interface BlastDbCatalogItem {
  value: string;
  label: string;
  desc: string;
  size: string;
  estFiles: number;
  estMinutes: string;
  category: "Small / Test" | "Medium" | "Large";
  type: "nucl" | "prot";
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
];

export function formatNcbiVersion(v: string | null | undefined): string {
  if (!v) return "";
  const match = v.match(/^(\d{4}-\d{2}-\d{2})-(\d{2})-(\d{2})-(\d{2})$/);
  if (!match) return v;
  return `${match[1]} ${match[2]}:${match[3]}:${match[4]}`;
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