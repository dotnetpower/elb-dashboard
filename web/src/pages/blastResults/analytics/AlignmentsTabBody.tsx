import { useEffect, useRef, useState } from "react";
import { Eye, Loader2 } from "lucide-react";

import { AlignmentViewer } from "./AlignmentViewer";
import { DegradedBanner } from "./DegradedBanner";
import { ResultFilterBar } from "./ResultFilterBar";
import { ResultsPendingPanel } from "./ResultsPendingPanel";
import { isPartialResult } from "./helpers";
import { hitKey } from "./useBlastAnalyticsState";
import type { BlastAnalyticsState } from "./useBlastAnalyticsState";

export interface AlignmentsTabBodyProps {
  analytics: BlastAnalyticsState;
  resultsPending?: boolean;
}

// Initial number of pairwise cards to mount, and how many more to mount
// per sentinel hit. Each AlignmentViewer renders one styled <span> per
// base in qseq/sseq, so a single page of 100 hits is ~100k DOM nodes.
// Mounting in batches keeps the first paint cheap; the rest stream in as
// the user scrolls.
const INITIAL_BATCH = 10;
const BATCH_STEP = 10;

/** Per-hit pairwise alignment view, mirrors NCBI's Alignments tab. */
export function AlignmentsTabBody({ analytics, resultsPending = false }: AlignmentsTabBodyProps) {
  const { alignQuery, alignments } = analytics;
  const filteredHitCount =
    alignQuery.data?.filtered_hits ?? alignQuery.data?.total_hits ?? 0;

  const [visibleCount, setVisibleCount] = useState(INITIAL_BATCH);
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  // Reset the window whenever the underlying alignments array changes
  // (page change, filter apply, refetch). Using array identity is enough
  // here — useBlastAnalyticsState returns a fresh array per query result.
  useEffect(() => {
    setVisibleCount(INITIAL_BATCH);
  }, [alignments]);

  // IntersectionObserver bumps the window when the sentinel scrolls into
  // view. rootMargin gives a small pre-fetch buffer so the next batch is
  // ready before the user actually reaches the bottom.
  useEffect(() => {
    const node = sentinelRef.current;
    if (!node) return;
    if (visibleCount >= alignments.length) return;
    if (typeof IntersectionObserver === "undefined") {
      // SSR / legacy fallback — render everything at once.
      setVisibleCount(alignments.length);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisibleCount((current) =>
            Math.min(current + BATCH_STEP, alignments.length),
          );
        }
      },
      { rootMargin: "400px 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [visibleCount, alignments.length]);

  if (resultsPending) {
    return <ResultsPendingPanel />;
  }

  const visibleAlignments = alignments.slice(0, visibleCount);
  const hasMore = visibleCount < alignments.length;

  return (
    <div>
      <ResultFilterBar
        analytics={analytics}
        onRefresh={() => alignQuery.refetch()}
      />

      {alignQuery.isLoading && (
        <div className="glass-card" style={{ padding: 40, textAlign: "center" }}>
          <Loader2 size={24} className="spin" style={{ color: "var(--accent)" }} />
          <p className="muted" style={{ marginTop: 12 }}>
            Loading alignments...
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

      {isPartialResult(alignQuery.data) && !resultsPending && (
        <DegradedBanner data={alignQuery.data} resultsPending={resultsPending} />
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
              Adjust the filters above or pick a broader database.
            </p>
          </div>
        )}

      {visibleAlignments.map((hit) => (
        <AlignmentViewer key={hitKey(hit)} hit={hit} />
      ))}

      {hasMore && (
        <div
          ref={sentinelRef}
          className="glass-card"
          style={{
            padding: 16,
            marginBottom: 12,
            textAlign: "center",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 8,
          }}
        >
          <Loader2 size={16} className="spin" style={{ color: "var(--accent)" }} />
          <span className="muted" style={{ fontSize: 12 }}>
            Showing {visibleCount} of {alignments.length} — loading more…
          </span>
        </div>
      )}

      {!hasMore && alignments.length > INITIAL_BATCH && (
        <div style={{ textAlign: "center", padding: "8px 0" }}>
          <span className="muted" style={{ fontSize: 12 }}>
            All {alignments.length} alignments shown.
          </span>
        </div>
      )}
    </div>
  );
}
