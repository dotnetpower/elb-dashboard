import type { ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  AlignLeft,
  BarChart3,
  FileSearch,
  FileText,
  Settings,
  TreePine,
} from "lucide-react";

const TABS: Array<{
  key: BlastResultsTab;
  label: string;
  subtitle: string;
  icon: ReactNode;
}> = [
  {
    key: "descriptions",
    label: "Descriptions",
    subtitle: "Sequences producing significant alignments",
    icon: <FileSearch size={14} strokeWidth={1.5} />,
  },
  {
    key: "graphic",
    label: "Graphic Summary",
    subtitle: "Distribution of hits across the query",
    icon: <BarChart3 size={14} strokeWidth={1.5} />,
  },
  {
    key: "alignments",
    label: "Alignments",
    subtitle: "Pairwise alignment for each hit",
    icon: <AlignLeft size={14} strokeWidth={1.5} />,
  },
  {
    key: "taxonomy",
    label: "Taxonomy",
    subtitle: "Organism rollup of the current hits",
    icon: <TreePine size={14} strokeWidth={1.5} />,
  },
  {
    key: "files",
    label: "Files",
    subtitle: "Raw output blobs in Azure Storage",
    icon: <FileText size={14} strokeWidth={1.5} />,
  },
  {
    key: "run",
    label: "Run details",
    subtitle: "Execution timeline and cluster details",
    icon: <Settings size={14} strokeWidth={1.5} />,
  },
];

export type BlastResultsTab =
  | "descriptions"
  | "graphic"
  | "alignments"
  | "taxonomy"
  | "files"
  | "run";

export const DEFAULT_BLAST_TAB: BlastResultsTab = "descriptions";

export function resolveBlastResultsTab(value: string | null): BlastResultsTab {
  switch (value) {
    case "descriptions":
    case "graphic":
    case "alignments":
    case "taxonomy":
    case "files":
    case "run":
      return value;
    default:
      return DEFAULT_BLAST_TAB;
  }
}

export interface BlastResultsTabsProps {
  active: BlastResultsTab;
}

/**
 * Sticky tab bar at the top of the BLAST search result page. Mirrors
 * NCBI's tab order — Descriptions / Graphic Summary / Alignments /
 * Taxonomy — and appends two ElasticBLAST-only tabs (Files and Run
 * details) for the operator-facing surface that NCBI does not provide.
 *
 * Active tab is encoded in the URL (`?tab=...`) so deep-links survive
 * page reloads and we keep React Router's back/forward behaviour for
 * the in-page navigation.
 */
export function BlastResultsTabs({ active }: BlastResultsTabsProps) {
  const [searchParams] = useSearchParams();

  return (
    <nav
      style={{
        position: "sticky",
        top: 0,
        zIndex: 5,
        background: "color-mix(in srgb, var(--bg-primary) 88%, transparent)",
        backdropFilter: "blur(10px)",
        borderBottom: "1px solid var(--glass-border)",
        margin: "0 calc(-1 * var(--space-3)) var(--space-3)",
        padding: "8px var(--space-3) 0",
        display: "flex",
        flexWrap: "wrap",
        gap: 2,
      }}
      aria-label="BLAST results sections"
    >
      {TABS.map((tab) => {
        const isActive = tab.key === active;
        const nextParams = new URLSearchParams(searchParams);
        nextParams.set("tab", tab.key);
        return (
          <Link
            key={tab.key}
            to={`?${nextParams.toString()}`}
            replace
            title={tab.subtitle}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "8px 14px 10px",
              fontSize: 13,
              color: isActive ? "var(--accent)" : "var(--text-muted)",
              borderBottom: isActive
                ? "2px solid var(--accent)"
                : "2px solid transparent",
              textDecoration: "none",
              fontWeight: isActive ? 600 : 500,
              transition: "color 150ms ease-out, border-color 150ms ease-out",
            }}
          >
            {tab.icon}
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
