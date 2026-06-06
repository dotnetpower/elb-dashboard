/**
 * diagnosticsModel — pure presentation helpers for the diagnostics page.
 *
 * Extracted from `DiagnosticsPage.tsx` so the grouping / ordering logic is unit
 * testable without rendering React. The page renders findings grouped by
 * resource kind, with the most-severe group (and finding) first.
 */
import { severityRank, type Finding } from "@/api/diagnostics";

export const RESOURCE_LABEL: Record<string, string> = {
  aks: "AKS cluster",
  storage: "Storage account",
  acr: "Container registry",
  container_app: "Control plane (Container App)",
  api: "API surface",
  queue: "BLAST jobs",
};

/**
 * Group findings by `resource_kind`, ordering groups by their most-severe
 * member (critical first). Findings within a group keep their input order;
 * the page sorts them by severity at render time.
 */
export function groupByResource(findings: Finding[]): [string, Finding[]][] {
  const map = new Map<string, Finding[]>();
  for (const f of findings) {
    const list = map.get(f.resource_kind) ?? [];
    list.push(f);
    map.set(f.resource_kind, list);
  }
  return [...map.entries()].sort((a, b) => {
    const aMax = Math.max(...a[1].map((f) => severityRank(f.severity)));
    const bMax = Math.max(...b[1].map((f) => severityRank(f.severity)));
    return bMax - aMax;
  });
}

/** Sort findings most-severe first (stable for equal severities). */
export function sortBySeverity(findings: Finding[]): Finding[] {
  return findings.slice().sort((a, b) => severityRank(b.severity) - severityRank(a.severity));
}
