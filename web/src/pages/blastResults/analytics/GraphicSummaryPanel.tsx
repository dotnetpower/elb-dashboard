import { useMemo } from "react";
import { Loader2 } from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";

import type { BlastHit } from "@/api/endpoints";
import {
  NCBI_SCORE_COLOR,
  NCBI_SCORE_LABEL,
  ncbiScoreBin,
  numberValue,
  type NcbiScoreBin,
} from "./helpers";
import { OverviewPanel } from "./OverviewPanel";
import type { BlastAnalyticsState } from "./useBlastAnalyticsState";

const SCORE_BIN_ORDER: NcbiScoreBin[] = [
  "<40",
  "40-50",
  "50-80",
  "80-200",
  ">=200",
];

export interface GraphicSummaryPanelProps {
  analytics: BlastAnalyticsState;
}

/**
 * NCBI Web BLAST's "Graphic Summary" view, ported.
 *
 * Lays the active query on a horizontal ruler (1 .. qlen) and stacks one
 * row per hit underneath, color-coded by the canonical BLAST bit-score
 * palette (<40 black, 40-50 blue, 50-80 green, 80-200 magenta, ≥200 red).
 * Hover shows the subject title, click opens the per-hit alignment.
 *
 * When the result set spans multiple queries we group by `qseqid` and
 * render one ruler per group (matching what NCBI does when you submit a
 * multi-FASTA — each query becomes its own block).
 */
export function GraphicSummaryPanel({ analytics }: GraphicSummaryPanelProps) {
  const { alignQuery, alignments, applyImmediate } = analytics;
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const groups = useMemo(() => groupHitsByQuery(alignments), [alignments]);

  /**
   * NCBI's behaviour: clicking a hit bar drops you into the Alignments
   * tab focused on that subject. We narrow by both the query (so the
   * Alignments tab only shows hits for the clicked query group) and the
   * subject accession, then swap the active tab via the URL.
   */
  const handleBarActivate = (hit: BlastHit) => {
    applyImmediate({
      queryFilter: hit.qseqid,
      subjectFilter: hit.sseqid,
    });
    const next = new URLSearchParams(searchParams);
    next.set("tab", "alignments");
    navigate(`?${next.toString()}`, { replace: false });
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <OverviewPanel analytics={analytics} />

      <div className="glass-card" style={{ padding: 16 }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            marginBottom: 12,
          }}
        >
          <h3 style={{ margin: 0, fontSize: 14 }}>Graphic summary</h3>
          <ScoreLegend />
        </div>

        {alignQuery.isLoading && (
          <div style={{ padding: 24, textAlign: "center" }}>
            <Loader2 size={20} className="spin" style={{ color: "var(--accent)" }} />
            <p className="muted" style={{ marginTop: 8 }}>
              Loading hits...
            </p>
          </div>
        )}

        {alignQuery.isError && (
          <p className="muted" style={{ color: "var(--danger)" }}>
            Failed to load hits: {(alignQuery.error as Error).message}
          </p>
        )}

        {!alignQuery.isLoading && groups.length === 0 && (
          <p className="muted">No hits to plot for the current filter set.</p>
        )}

        {groups.map((group) => (
          <QueryRuler
            key={group.qseqid}
            group={group}
            onBarActivate={handleBarActivate}
          />
        ))}
      </div>
    </div>
  );
}

interface HitGroup {
  qseqid: string;
  qlen: number;
  hits: BlastHit[];
}

function groupHitsByQuery(hits: BlastHit[]): HitGroup[] {
  const map = new Map<string, HitGroup>();
  for (const hit of hits) {
    const qseqid = hit.qseqid || "Query";
    const existing = map.get(qseqid);
    const qlen = inferQueryLen(hit) ?? existing?.qlen ?? 0;
    if (existing) {
      existing.hits.push(hit);
      if (qlen > existing.qlen) existing.qlen = qlen;
    } else {
      map.set(qseqid, { qseqid, qlen, hits: [hit] });
    }
  }
  return [...map.values()].sort((a, b) => a.qseqid.localeCompare(b.qseqid));
}

function inferQueryLen(hit: BlastHit): number | null {
  const value = numberValue(hit.qlen);
  if (value !== null && value > 0) return value;
  const qend = numberValue(hit.qend);
  if (qend !== null && qend > 0) return qend;
  return null;
}

function QueryRuler({
  group,
  onBarActivate,
}: {
  group: HitGroup;
  onBarActivate: (hit: BlastHit) => void;
}) {
  const qlen = Math.max(group.qlen, 1);
  // Sort hits high-score first so the strongest bars render at the top —
  // matches NCBI's vertical ordering.
  const sortedHits = [...group.hits].sort(
    (a, b) => (numberValue(b.bitscore) ?? 0) - (numberValue(a.bitscore) ?? 0),
  );

  const tickPositions = computeTickPositions(qlen);

  return (
    <div style={{ marginBottom: 18 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          fontSize: 12,
          color: "var(--text-muted)",
          marginBottom: 6,
        }}
      >
        <span style={{ fontWeight: 600, color: "var(--text-primary)" }}>
          {group.qseqid}
        </span>
        <span>
          ({sortedHits.length} hit{sortedHits.length === 1 ? "" : "s"} · query length{" "}
          {group.qlen.toLocaleString()})
        </span>
      </div>

      <div
        style={{
          position: "relative",
          paddingTop: 18,
          paddingBottom: 8,
          background: "var(--bg-tertiary)",
          borderRadius: 6,
        }}
      >
        {/* Query ruler ticks */}
        <div
          style={{
            position: "relative",
            height: 14,
            margin: "0 8px 4px",
          }}
        >
          {tickPositions.map((tick) => (
            <div
              key={tick.position}
              style={{
                position: "absolute",
                left: `${(tick.position / qlen) * 100}%`,
                transform: "translateX(-50%)",
                fontSize: 10,
                color: "var(--text-muted)",
                whiteSpace: "nowrap",
              }}
            >
              {tick.label}
            </div>
          ))}
        </div>
        <div
          style={{
            position: "relative",
            height: 4,
            margin: "0 8px 8px",
            background: "color-mix(in srgb, var(--accent) 28%, transparent)",
            borderRadius: 2,
          }}
        />

        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 2,
            margin: "0 8px",
          }}
        >
          {sortedHits.map((hit, index) => (
            <HitBar
              key={`${hit.sseqid}-${index}`}
              hit={hit}
              qlen={qlen}
              onActivate={onBarActivate}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function HitBar({
  hit,
  qlen,
  onActivate,
}: {
  hit: BlastHit;
  qlen: number;
  onActivate: (hit: BlastHit) => void;
}) {
  const qstart = numberValue(hit.qstart);
  const qend = numberValue(hit.qend);
  if (qstart === null || qend === null || qlen <= 0) return null;

  const left = (Math.max(0, Math.min(qstart, qend) - 1) / qlen) * 100;
  const width = Math.max(0.4, (Math.abs(qend - qstart) + 1) / qlen) * 100;
  const color = NCBI_SCORE_COLOR[ncbiScoreBin(hit.bitscore)];
  const title = `${hit.sseqid}${hit.stitle ? ` · ${hit.stitle}` : ""} — bit=${
    numberValue(hit.bitscore)?.toFixed(1) ?? "—"
  }, E=${hit.evalue} · click to open in Alignments tab`;

  return (
    <button
      type="button"
      title={title}
      onClick={() => onActivate(hit)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onActivate(hit);
        }
      }}
      style={{
        position: "relative",
        display: "block",
        height: 8,
        padding: 0,
        margin: 0,
        background: "transparent",
        border: 0,
        cursor: "pointer",
        width: "100%",
      }}
      aria-label={`Open alignment for ${hit.sseqid} in the Alignments tab`}
    >
      <span
        style={{
          position: "absolute",
          left: `${left}%`,
          width: `${Math.min(100, width)}%`,
          top: 0,
          bottom: 0,
          background: color,
          borderRadius: 1,
          display: "block",
          minWidth: 2,
          boxShadow: "0 0 0 1px rgba(0,0,0,0.15)",
        }}
      />
    </button>
  );
}

function computeTickPositions(qlen: number): Array<{ position: number; label: string }> {
  // Aim for ~6 evenly spaced ticks across the query, including the ends.
  const ticks: Array<{ position: number; label: string }> = [];
  const desired = 6;
  const step = qlen / (desired - 1);
  for (let i = 0; i < desired; i++) {
    const position = Math.round(i * step);
    ticks.push({ position, label: position.toLocaleString() });
  }
  return ticks;
}

function ScoreLegend() {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        flexWrap: "wrap",
        fontSize: 11,
        color: "var(--text-muted)",
      }}
    >
      <span>Alignment score:</span>
      {SCORE_BIN_ORDER.map((bin) => (
        <span
          key={bin}
          style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
        >
          <span
            style={{
              width: 16,
              height: 10,
              background: NCBI_SCORE_COLOR[bin],
              borderRadius: 2,
              display: "inline-block",
              boxShadow: "0 0 0 1px rgba(0,0,0,0.2)",
            }}
          />
          {NCBI_SCORE_LABEL[bin]}
        </span>
      ))}
    </div>
  );
}
