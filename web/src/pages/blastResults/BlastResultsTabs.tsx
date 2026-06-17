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

import { QUEUED_PHASES } from "@/constants";

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

const RESULT_TABS = new Set<BlastResultsTab>([
  "descriptions",
  "graphic",
  "alignments",
  "taxonomy",
  "files",
]);

const RESULT_ANALYTICS_TABS = new Set<BlastResultsTab>([
  "descriptions",
  "graphic",
  "alignments",
  "taxonomy",
]);

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

export function shouldOpenRunDetailsForFailedJob(
  activeTab: BlastResultsTab,
  effectiveIsFailed: boolean,
): boolean {
  return effectiveIsFailed && RESULT_ANALYTICS_TABS.has(activeTab);
}

/**
 * Label + tone for the in-progress badge on result tabs. A queued-family phase
 * reads a calm grey "Queued" so it matches the header phase banner and the Job
 * Details status dot (both already collapse to "queued"); every other active
 * phase keeps the accent "Running". Pure so it stays unit-testable.
 */
export function resultTabBadge(effectivePhase: string): { label: string; color: string } {
  return QUEUED_PHASES.has(effectivePhase)
    ? { label: "Queued", color: "var(--text-muted)" }
    : { label: "Running", color: "var(--accent)" };
}

export interface BlastResultsTabsProps {
  active: BlastResultsTab;
  resultsPending?: boolean;
  /**
   * The job's effective phase, used to label the in-progress tab badge. When
   * the phase is a queued-family phase the badge reads a calm grey "Queued"
   * instead of the accent "Running", so it matches the header phase banner and
   * the Job Details status dot (which both already collapse to "queued").
   */
  effectivePhase?: string;
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
export function BlastResultsTabs({
  active,
  resultsPending = false,
  effectivePhase = "",
}: BlastResultsTabsProps) {
  const [searchParams] = useSearchParams();
  // A queued job is still "active" (resultsPending), but its badge must read
  // "Queued" in the calm grey tone rather than the accent "Running" so every
  // surface on the page tells the same story.
  const { label: badgeLabel, color: badgeColor } = resultTabBadge(effectivePhase);

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
            {resultsPending && RESULT_TABS.has(tab.key) && (
              <span
                style={{
                  marginLeft: 2,
                  padding: "1px 5px",
                  borderRadius: 999,
                  border: `1px solid color-mix(in srgb, ${badgeColor} 35%, transparent)`,
                  color: badgeColor,
                  fontSize: 10,
                  fontWeight: 600,
                  lineHeight: 1.4,
                }}
              >
                {badgeLabel}
              </span>
            )}
          </Link>
        );
      })}
    </nav>
  );
}
