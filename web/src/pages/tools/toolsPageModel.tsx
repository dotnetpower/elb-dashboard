import type { ReactNode } from "react";
import { Calendar, Clock, DollarSign, FlaskConical, Scissors, Search, Shield } from "lucide-react";

export type TabKey =
  | "cost"
  | "preprocess"
  | "primer"
  | "taxonomy"
  | "schedules"
  | "versions"
  | "audit";

export interface TabMeta {
  key: TabKey;
  label: string;
  icon: ReactNode;
  desc: string;
  needsConfig?: boolean;
}

export const TAB_GROUPS: { label: string; tabs: TabMeta[] }[] = [
  {
    label: "Plan",
    tabs: [
      {
        key: "cost",
        label: "Cost Estimator",
        icon: <DollarSign size={13} />,
        desc: "Predict Azure spend before running a BLAST job",
      },
    ],
  },
  {
    label: "Sequence",
    tabs: [
      {
        key: "preprocess",
        label: "Preprocessor",
        icon: <Scissors size={13} />,
        desc: "Convert FASTQ → FASTA, filter by length and quality",
      },
      {
        key: "primer",
        label: "Primer Design",
        icon: <FlaskConical size={13} />,
        desc: "Run Primer3 on the Remote Terminal VM",
        needsConfig: true,
      },
      {
        key: "taxonomy",
        label: "Taxonomy",
        icon: <Search size={13} />,
        desc: "Annotate hit accessions with NCBI organism metadata",
      },
    ],
  },
  {
    label: "Operations",
    tabs: [
      {
        key: "schedules",
        label: "Schedules",
        icon: <Calendar size={13} />,
        desc: "Saved configurations for one-click or scheduled BLAST runs",
      },
      {
        key: "versions",
        label: "DB Versions",
        icon: <Clock size={13} />,
        desc: "Track database provenance across your storage account",
        needsConfig: true,
      },
      {
        key: "audit",
        label: "Audit Trail",
        icon: <Shield size={13} />,
        desc: "Immutable log of operations for GLP / CLIA compliance",
      },
    ],
  },
];

export const TAB_INDEX: Record<TabKey, TabMeta> = TAB_GROUPS.reduce(
  (acc, group) => {
    for (const tab of group.tabs) acc[tab.key] = tab;
    return acc;
  },
  {} as Record<TabKey, TabMeta>,
);