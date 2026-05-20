import { Eye, Loader2 } from "lucide-react";

import { AlignmentViewer } from "./AlignmentViewer";
import { DegradedBanner } from "./DegradedBanner";
import { ResultFilterBar } from "./ResultFilterBar";
import { isPartialResult } from "./helpers";
import { hitKey } from "./useBlastAnalyticsState";
import type { BlastAnalyticsState } from "./useBlastAnalyticsState";

export interface AlignmentsTabBodyProps {
  analytics: BlastAnalyticsState;
  resultsPending?: boolean;
}

/** Per-hit pairwise alignment view, mirrors NCBI's Alignments tab. */
export function AlignmentsTabBody({ analytics, resultsPending = false }: AlignmentsTabBodyProps) {
  const { alignQuery, alignments } = analytics;
  const filteredHitCount =
    alignQuery.data?.filtered_hits ?? alignQuery.data?.total_hits ?? 0;

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

      {isPartialResult(alignQuery.data) && (
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

      {alignments.map((hit) => (
        <AlignmentViewer key={hitKey(hit)} hit={hit} />
      ))}
    </div>
  );
}
