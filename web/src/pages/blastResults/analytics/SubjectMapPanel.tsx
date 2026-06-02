import { useMemo, useState } from "react";
import { AlertTriangle } from "lucide-react";

import type { BlastHit } from "@/api/endpoints";
import { formatEvalue } from "./helpers";
import { buildSubjectTracks, type SubjectTrack } from "./derived";
import type { BlastAnalyticsState } from "./useBlastAnalyticsState";

export interface SubjectMapPanelProps {
  analytics: BlastAnalyticsState;
  onHitActivate: (hit: BlastHit) => void;
}

const STRAND_COLOR = { plus: "#4a78ff", minus: "#c850b0" } as const;
const MAX_TRACKS = 12;

/**
 * Subject coordinate map — lays each subject's HSPs on the subject axis
 * (1..slen) instead of the query axis NCBI's Graphic Summary uses. The
 * extra view surfaces structure the query-centric ruler hides: multiple
 * HSPs tiling one subject, strand flips, and subject-order inversions
 * (rearrangements / duplications / repeats). Only subjects with at least
 * one HSP are shown; subjects with a structural signal are flagged and
 * sorted to the top.
 */
export function SubjectMapPanel({ analytics, onHitActivate }: SubjectMapPanelProps) {
  const { alignments } = analytics;
  const tracks = useMemo(() => buildSubjectTracks(alignments), [alignments]);
  const [showAll, setShowAll] = useState(false);

  // Multi-HSP subjects are where the subject view earns its keep; sort the
  // flagged/structural ones first so they are not buried.
  const ranked = useMemo(() => {
    return [...tracks].sort((a, b) => {
      const aFlag = (a.hasStrandFlip || a.hasOrderInversion) && a.hspCount > 1 ? 1 : 0;
      const bFlag = (b.hasStrandFlip || b.hasOrderInversion) && b.hspCount > 1 ? 1 : 0;
      if (aFlag !== bFlag) return bFlag - aFlag;
      return b.hspCount - a.hspCount;
    });
  }, [tracks]);

  const multiHspCount = useMemo(
    () => tracks.filter((t) => t.hspCount > 1).length,
    [tracks],
  );

  // The subject map earns its place only when at least one subject carries
  // multiple HSPs — that is where strand flips / inversions live. With every
  // subject at a single HSP the view would just duplicate the query-centric
  // Graphic Summary with single bars, so stay out of the way entirely.
  if (ranked.length === 0 || multiHspCount === 0) return null;

  const visible = showAll ? ranked : ranked.slice(0, MAX_TRACKS);

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
        <h3 style={{ margin: 0, fontSize: 14 }}>Subject coordinate map</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          {multiHspCount} subject{multiHspCount === 1 ? "" : "s"} with multiple HSPs
        </span>
      </div>
      <p className="muted" style={{ margin: "0 0 12px", fontSize: 12 }}>
        HSPs plotted along each subject (1..length). Blue = plus strand, magenta = minus.
        Flagged subjects show a strand flip or coordinate inversion.
      </p>

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {visible.map((track) => (
          <SubjectTrackRow key={track.sseqid} track={track} onHitActivate={onHitActivate} />
        ))}
      </div>

      {ranked.length > MAX_TRACKS && (
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          style={{ marginTop: 12 }}
          onClick={() => setShowAll((v) => !v)}
        >
          {showAll ? "Show fewer" : `Show all ${ranked.length} subjects`}
        </button>
      )}
    </div>
  );
}

function SubjectTrackRow({
  track,
  onHitActivate,
}: {
  track: SubjectTrack;
  onHitActivate: (hit: BlastHit) => void;
}) {
  const slen = Math.max(track.slen, 1);
  const flagged = (track.hasStrandFlip || track.hasOrderInversion) && track.hspCount > 1;

  return (
    <div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          fontSize: 12,
          marginBottom: 4,
        }}
      >
        <code className="code-val" style={{ wordBreak: "break-all", fontWeight: 600 }}>
          {track.sseqid}
        </code>
        <span className="muted">
          {track.hspCount} HSP{track.hspCount === 1 ? "" : "s"} · {slen.toLocaleString()} bp
        </span>
        {flagged && (
          <span
            title={[
              track.hasStrandFlip ? "Strand flip" : null,
              track.hasOrderInversion ? "Coordinate inversion" : null,
            ]
              .filter(Boolean)
              .join(" · ")}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              fontSize: 11,
              padding: "1px 7px",
              borderRadius: 999,
              background: "color-mix(in srgb, var(--warning) 16%, transparent)",
              color: "var(--warning)",
              fontWeight: 600,
            }}
          >
            <AlertTriangle size={11} />
            {track.hasOrderInversion ? "Inversion" : "Strand flip"}
          </span>
        )}
      </div>

      <div
        style={{
          position: "relative",
          height: 16,
          borderRadius: 4,
          background: "var(--bg-tertiary)",
          overflow: "hidden",
        }}
      >
        {track.hsps.map((hsp, index) => {
          const lo = Math.min(hsp.sstart, hsp.send);
          const hi = Math.max(hsp.sstart, hsp.send);
          const left = ((lo - 1) / slen) * 100;
          const width = Math.max(((hi - lo + 1) / slen) * 100, 0.6);
          return (
            <button
              key={`${hsp.hit.sseqid}-${index}`}
              type="button"
              title={`${hsp.strand} strand · ${lo.toLocaleString()}–${hi.toLocaleString()} · E ${formatEvalue(hsp.hit.evalue)}`}
              onClick={() => onHitActivate(hsp.hit)}
              style={{
                position: "absolute",
                left: `${left}%`,
                width: `${width}%`,
                top: 3,
                height: 10,
                border: "none",
                borderRadius: 3,
                background: STRAND_COLOR[hsp.strand],
                opacity: 0.85,
                cursor: "pointer",
              }}
            />
          );
        })}
      </div>
    </div>
  );
}
