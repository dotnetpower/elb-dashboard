import { Eye, Loader2, RefreshCw } from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { BlastHitsTable } from "./BlastHitsTable";
import { DegradedBanner } from "./DegradedBanner";
import { ResultFilterBar } from "./ResultFilterBar";
import { isPartialResult, isResultFilesUnavailable, ncbiNuccoreUrl } from "./helpers";
import type { BlastAnalyticsState } from "./useBlastAnalyticsState";
import type { BlastHit } from "@/api/endpoints";

export interface DescriptionsTabBodyProps {
  analytics: BlastAnalyticsState;
  resultsPending?: boolean;
}

/**
 * The Descriptions tab — NCBI's primary "Sequences producing significant
 * alignments" view. Combines the NCBI-style filter bar, the hits table
 * with bulk selection, the degraded banner, and a friendly empty state.
 */
export function DescriptionsTabBody({ analytics, resultsPending = false }: DescriptionsTabBodyProps) {
  const { alignQuery, alignments, applyImmediate } = analytics;
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const filteredHitCount =
    alignQuery.data?.filtered_hits ?? alignQuery.data?.total_hits ?? 0;
  const resultFilesUnavailable = isResultFilesUnavailable(alignQuery.data);

  /**
   * Multi-HSP indicator → Alignments tab deep-link. Mirrors the
   * Graphic Summary handler so clicking the `Max / Total (N HSPs)`
   * indicator narrows the Alignments view to that subject. NCBI's
   * equivalent is "Number of Matches: N → Next Match" on the
   * Alignments tab; this brings users there with the right hit picked.
   */
  const handleSubjectDrilldown = (hit: BlastHit) => {
    applyImmediate({
      queryFilter: hit.qseqid,
      subjectFilter: hit.sseqid,
    });
    const next = new URLSearchParams(searchParams);
    next.set("tab", "alignments");
    navigate(`?${next.toString()}`, { replace: false });
  };

  return (
    <div>
      {!resultFilesUnavailable && (
        <ResultFilterBar analytics={analytics} onRefresh={() => alignQuery.refetch()} />
      )}

      {alignQuery.isLoading && (
        <div className="glass-card" style={{ padding: 40, textAlign: "center" }}>
          <Loader2 size={24} className="spin" style={{ color: "var(--accent)" }} />
          <p className="muted" style={{ marginTop: 12 }}>
            Loading BLAST hits...
          </p>
        </div>
      )}

      {alignQuery.isError && (
        <div className="glass-card" style={{ padding: 20, borderColor: "var(--danger)" }}>
          <p style={{ color: "var(--danger)" }}>
            Failed: {(alignQuery.error as Error).message}
          </p>
        </div>
      )}

      {isPartialResult(alignQuery.data) && (
        <DegradedBanner data={alignQuery.data} resultsPending={resultsPending} />
      )}

      {alignments.length === 0 && !alignQuery.isLoading && resultFilesUnavailable && (
        <div className="glass-card" style={{ padding: 24, textAlign: "center" }}>
          <Eye size={32} className="muted" style={{ marginBottom: 8 }} />
          <p style={{ color: "var(--text-primary)", margin: "0 0 4px" }}>
            {resultsPending ? "Result files are still being prepared." : "Result files are not available."}
          </p>
          <p className="muted" style={{ margin: 0, fontSize: 12 }}>
            {resultsPending
              ? "BLAST is still running or exporting final output. This view will update when parseable result files appear."
              : "The job record is available, but the API could not read parseable BLAST output files for this result yet."}
          </p>
          <button
            className="btn btn--ghost btn--sm"
            onClick={() => alignQuery.refetch()}
            disabled={alignQuery.isFetching}
            style={{ marginTop: 14 }}
          >
            <RefreshCw size={14} className={alignQuery.isFetching ? "spin" : ""} />
            Retry
          </button>
        </div>
      )}

      {alignments.length === 0 &&
        !alignQuery.isLoading &&
        !isPartialResult(alignQuery.data) &&
        filteredHitCount === 0 && (
          <div className="glass-card" style={{ padding: 24, textAlign: "center" }}>
            <Eye size={32} className="muted" style={{ marginBottom: 8 }} />
            <p style={{ color: "var(--text-primary)", margin: "0 0 4px" }}>
              No significant similarity found.
            </p>
            <p className="muted" style={{ margin: 0, fontSize: 12 }}>
              BLAST did not return any hits matching the current filters. Try lowering the
              identity threshold or raising the maximum E-value.
            </p>
          </div>
        )}

      {alignments.length > 0 && (
        <BlastHitsTable
          hits={alignments}
          analytics={analytics}
          onSendToMsa={sendSelectedToMsa}
          onDownloadSelection={downloadAccessionList}
          onSubjectDrilldown={handleSubjectDrilldown}
        />
      )}
    </div>
  );
}

/**
 * Opens the NCBI MSA Viewer with the selected accessions. NCBI's MSA
 * Viewer accepts `?coltype=GenBank&ids=acc1,acc2,...` so we build that
 * URL and pop a tab — same UX as their built-in batch action.
 */
function sendSelectedToMsa(hits: BlastHit[]) {
  if (hits.length === 0) return;
  const ids = hits
    .map((hit) => hit.sseqid.split("|").pop()?.split(".")[0] ?? hit.sseqid)
    .filter(Boolean)
    .slice(0, 50)
    .join(",");
  const url = `https://www.ncbi.nlm.nih.gov/projects/msaviewer/?coltype=GenBank&ids=${encodeURIComponent(
    ids,
  )}`;
  window.open(url, "_blank", "noopener,noreferrer");
}

/**
 * Quick "Download selection" handler — builds a CSV of `accession,
 * organism, nuccore URL` so the researcher can paste it into Excel or
 * pipe it into the next analysis. Avoids hitting the backend (which
 * doesn't yet expose a per-hit FASTA endpoint).
 */
function downloadAccessionList(hits: BlastHit[]) {
  if (hits.length === 0) return;
  const header = "accession,organism,nuccore_url\n";
  const body = hits
    .map((hit) => {
      const accession = hit.sseqid.replace(/,/g, " ");
      const organism = (hit.sscinames ?? "").replace(/,/g, " ");
      return `${accession},${organism},${ncbiNuccoreUrl(hit.sseqid)}`;
    })
    .join("\n");
  const blob = new Blob([header + body], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `blast-selection-${hits.length}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
