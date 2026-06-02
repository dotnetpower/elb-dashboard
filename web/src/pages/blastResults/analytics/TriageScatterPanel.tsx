import { useMemo, useState } from "react";

import type { BlastHit } from "@/api/endpoints";
import { formatEvalue, formatPercent } from "./helpers";
import {
  REVIEW_BUCKET_COLOR,
  REVIEW_BUCKET_LABEL,
  TRIAGE_COVERAGE_THRESHOLD,
  TRIAGE_IDENTITY_THRESHOLD,
  TRIAGE_QUADRANT_LABEL,
  buildTriagePoints,
  triageQuadrantCounts,
  type ReviewBucket,
  type TriagePoint,
} from "./derived";
import type { BlastAnalyticsState } from "./useBlastAnalyticsState";

export interface TriageScatterPanelProps {
  analytics: BlastAnalyticsState;
  /** Deep-link a clicked point into the Alignments tab. */
  onHitActivate: (hit: BlastHit) => void;
}

const PLOT = { width: 520, height: 360, padL: 48, padR: 16, padT: 16, padB: 40 };
const REVIEW_ORDER: ReviewBucket[] = [
  "strong",
  "review",
  "low",
  "weak",
  "unclassified",
];

/**
 * Coverage × Identity triage scatter — a view NCBI does not provide.
 *
 * Each hit is a point at (query coverage, % identity); point area scales
 * with bit score and color encodes the review-status bucket. Threshold
 * guides split the plane into four quadrants so a researcher can read the
 * shape of a result set at a glance: the top-right cluster is the ortholog
 * candidates, top-left are partial/domain matches, bottom-right are
 * divergent homologs, bottom-left are marginal. Clicking a point deep-links
 * into the Alignments tab for that hit.
 */
export function TriageScatterPanel({ analytics, onHitActivate }: TriageScatterPanelProps) {
  const { alignments } = analytics;
  const points = useMemo(() => buildTriagePoints(alignments), [alignments]);
  const counts = useMemo(() => triageQuadrantCounts(points), [points]);
  const [hovered, setHovered] = useState<TriagePoint | null>(null);

  const maxBits = useMemo(
    () => points.reduce((m, p) => Math.max(m, p.bitscore), 0) || 1,
    [points],
  );

  if (points.length === 0) {
    return null;
  }

  const innerW = PLOT.width - PLOT.padL - PLOT.padR;
  const innerH = PLOT.height - PLOT.padT - PLOT.padB;
  const x = (qcovs: number) => PLOT.padL + (qcovs / 100) * innerW;
  const y = (pident: number) => PLOT.padT + (1 - pident / 100) * innerH;
  const radius = (bits: number) => 3 + Math.sqrt(bits / maxBits) * 7;

  return (
    <div className="glass-card" style={{ padding: 16 }}>
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: 12,
          marginBottom: 4,
        }}
      >
        <h3 style={{ margin: 0, fontSize: 14 }}>Coverage × Identity triage</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          {points.length} hits · point size = bit score
        </span>
      </div>
      <p className="muted" style={{ margin: "0 0 12px", fontSize: 12 }}>
        Top-right = ortholog candidates. Click a point to open its alignment.
      </p>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 20 }}>
        <div style={{ position: "relative" }}>
          <svg
            width={PLOT.width}
            height={PLOT.height}
            role="img"
            aria-label="Coverage versus identity scatter plot"
            style={{ maxWidth: "100%", height: "auto" }}
          >
            {/* quadrant shading + threshold guides */}
            <line
              x1={x(TRIAGE_COVERAGE_THRESHOLD)}
              y1={PLOT.padT}
              x2={x(TRIAGE_COVERAGE_THRESHOLD)}
              y2={PLOT.padT + innerH}
              stroke="var(--glass-border)"
              strokeDasharray="4 4"
            />
            <line
              x1={PLOT.padL}
              y1={y(TRIAGE_IDENTITY_THRESHOLD)}
              x2={PLOT.padL + innerW}
              y2={y(TRIAGE_IDENTITY_THRESHOLD)}
              stroke="var(--glass-border)"
              strokeDasharray="4 4"
            />
            {/* axes */}
            <line
              x1={PLOT.padL}
              y1={PLOT.padT + innerH}
              x2={PLOT.padL + innerW}
              y2={PLOT.padT + innerH}
              stroke="var(--text-muted)"
              strokeWidth={1}
            />
            <line
              x1={PLOT.padL}
              y1={PLOT.padT}
              x2={PLOT.padL}
              y2={PLOT.padT + innerH}
              stroke="var(--text-muted)"
              strokeWidth={1}
            />
            {[0, 25, 50, 75, 100].map((tick) => (
              <g key={`x-${tick}`}>
                <text
                  x={x(tick)}
                  y={PLOT.padT + innerH + 16}
                  fontSize={9}
                  fill="var(--text-muted)"
                  textAnchor="middle"
                >
                  {tick}
                </text>
              </g>
            ))}
            {[0, 25, 50, 75, 100].map((tick) => (
              <g key={`y-${tick}`}>
                <text
                  x={PLOT.padL - 6}
                  y={y(tick) + 3}
                  fontSize={9}
                  fill="var(--text-muted)"
                  textAnchor="end"
                >
                  {tick}
                </text>
              </g>
            ))}
            <text
              x={PLOT.padL + innerW / 2}
              y={PLOT.height - 4}
              fontSize={11}
              fill="var(--text-muted)"
              textAnchor="middle"
            >
              Query coverage (%)
            </text>
            <text
              x={12}
              y={PLOT.padT + innerH / 2}
              fontSize={11}
              fill="var(--text-muted)"
              textAnchor="middle"
              transform={`rotate(-90 12 ${PLOT.padT + innerH / 2})`}
            >
              Identity (%)
            </text>

            {points.map((point, index) => (
              <circle
                key={`${point.hit.qseqid}-${point.hit.sseqid}-${index}`}
                cx={x(point.qcovs)}
                cy={y(point.pident)}
                r={radius(point.bitscore)}
                fill={REVIEW_BUCKET_COLOR[point.bucket]}
                fillOpacity={0.55}
                stroke={REVIEW_BUCKET_COLOR[point.bucket]}
                strokeOpacity={0.9}
                style={{ cursor: "pointer" }}
                onMouseEnter={() => setHovered(point)}
                onMouseLeave={() => setHovered((h) => (h === point ? null : h))}
                onClick={() => onHitActivate(point.hit)}
              />
            ))}
          </svg>

          {hovered && (
            <div
              style={{
                position: "absolute",
                top: 8,
                right: 8,
                maxWidth: 220,
                padding: "8px 10px",
                borderRadius: 8,
                background: "color-mix(in srgb, var(--bg-primary) 92%, transparent)",
                border: "1px solid var(--glass-border)",
                fontSize: 11,
                pointerEvents: "none",
              }}
            >
              <div style={{ fontWeight: 600, marginBottom: 2, wordBreak: "break-all" }}>
                {hovered.hit.sseqid}
              </div>
              <div className="muted">Coverage {formatPercent(hovered.qcovs)}</div>
              <div className="muted">Identity {formatPercent(hovered.pident)}</div>
              <div className="muted">Bit score {hovered.bitscore.toFixed(0)}</div>
              <div className="muted">E = {formatEvalue(hovered.hit.evalue)}</div>
            </div>
          )}
        </div>

        <div style={{ minWidth: 180, display: "flex", flexDirection: "column", gap: 10 }}>
          <div>
            <div className="muted" style={{ fontSize: 11, marginBottom: 6, textTransform: "uppercase", letterSpacing: 0.4 }}>
              Quadrants
            </div>
            {(["ortholog", "partial", "divergent", "marginal"] as const).map((q) => (
              <div
                key={q}
                style={{ display: "flex", justifyContent: "space-between", gap: 8, fontSize: 12, marginBottom: 3 }}
                title={TRIAGE_QUADRANT_LABEL[q]}
              >
                <span style={{ color: "var(--text-muted)" }}>{TRIAGE_QUADRANT_LABEL[q].split(" (")[0]}</span>
                <span style={{ fontWeight: 600 }}>{counts[q]}</span>
              </div>
            ))}
          </div>
          <div>
            <div className="muted" style={{ fontSize: 11, marginBottom: 6, textTransform: "uppercase", letterSpacing: 0.4 }}>
              Review status
            </div>
            {REVIEW_ORDER.map((bucket) => (
              <div key={bucket} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, marginBottom: 3 }}>
                <span
                  style={{
                    width: 10,
                    height: 10,
                    borderRadius: 999,
                    background: REVIEW_BUCKET_COLOR[bucket],
                    display: "inline-block",
                  }}
                />
                <span style={{ color: "var(--text-muted)" }}>{REVIEW_BUCKET_LABEL[bucket]}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
