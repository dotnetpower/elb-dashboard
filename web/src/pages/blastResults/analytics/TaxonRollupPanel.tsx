import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Layers } from "lucide-react";

import type { BlastHit } from "@/api/endpoints";
import { formatEvalue, formatPercent, identityColor } from "./helpers";
import { derepByRank, type DerepRank, type TaxonRollupRow } from "./derived";
import type { BlastAnalyticsState } from "./useBlastAnalyticsState";

export interface TaxonRollupPanelProps {
  analytics: BlastAnalyticsState;
  /** Deep-link the best hit of a taxon into the Alignments tab. */
  onHitActivate: (hit: BlastHit) => void;
}

const RANKS: Array<{ key: DerepRank; label: string }> = [
  { key: "species", label: "Species" },
  { key: "genus", label: "Genus" },
];

/**
 * Taxonomic dereplication — collapse the (often redundant) hit list to one
 * representative per taxon so a researcher sees *which organisms* matched,
 * not 40 near-identical strains of the same species. NCBI's Taxonomy tab
 * counts hits per organism but does not let you fold the descriptions list
 * down to best-per-taxon and expand on demand; this does.
 *
 * Operates on the currently-filtered page of alignments (same set the
 * Descriptions table shows), so the rollup respects the active filters.
 */
export function TaxonRollupPanel({ analytics, onHitActivate }: TaxonRollupPanelProps) {
  const { alignments } = analytics;
  const [rank, setRank] = useState<DerepRank>("species");
  const [expanded, setExpanded] = useState(false);
  const [openRows, setOpenRows] = useState<Set<string>>(new Set());

  const rows = useMemo(() => derepByRank(alignments, rank), [alignments, rank]);

  if (alignments.length === 0) return null;

  const toggleRow = (key: string) => {
    setOpenRows((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const redundancy = alignments.length - rows.length;

  return (
    <div className="glass-card" style={{ padding: 16, marginBottom: 12 }}>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          width: "100%",
          background: "none",
          border: "none",
          padding: 0,
          cursor: "pointer",
          color: "var(--text-primary)",
        }}
      >
        {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        <Layers size={15} strokeWidth={1.5} style={{ color: "var(--accent)" }} />
        <span style={{ fontSize: 14, fontWeight: 600 }}>Taxonomic dereplication</span>
        <span className="muted" style={{ fontSize: 12, marginLeft: "auto" }}>
          {rows.length} taxa
          {redundancy > 0 ? ` · folds ${redundancy} redundant hit${redundancy === 1 ? "" : "s"}` : ""}
        </span>
      </button>

      {expanded && (
        <div style={{ marginTop: 14 }}>
          <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
            {RANKS.map((option) => (
              <button
                key={option.key}
                type="button"
                onClick={() => setRank(option.key)}
                className="btn btn--sm"
                style={{
                  background:
                    rank === option.key
                      ? "color-mix(in srgb, var(--accent) 18%, transparent)"
                      : "transparent",
                  border: "1px solid var(--glass-border)",
                  color: rank === option.key ? "var(--accent)" : "var(--text-muted)",
                  fontWeight: rank === option.key ? 600 : 500,
                }}
              >
                {option.label}
              </button>
            ))}
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {rows.map((row) => (
              <TaxonRow
                key={row.key}
                row={row}
                open={openRows.has(row.key)}
                onToggle={() => toggleRow(row.key)}
                onHitActivate={onHitActivate}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function TaxonRow({
  row,
  open,
  onToggle,
  onHitActivate,
}: {
  row: TaxonRollupRow;
  open: boolean;
  onToggle: () => void;
  onHitActivate: (hit: BlastHit) => void;
}) {
  const hasMore = row.hitCount > 1;
  return (
    <div
      style={{
        border: "1px solid var(--glass-border)",
        borderRadius: 8,
        padding: "8px 12px",
        background: "var(--bg-tertiary)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <button
          type="button"
          onClick={onToggle}
          disabled={!hasMore}
          aria-label={open ? "Collapse members" : "Expand members"}
          style={{
            background: "none",
            border: "none",
            padding: 0,
            cursor: hasMore ? "pointer" : "default",
            color: hasMore ? "var(--text-muted)" : "transparent",
            display: "inline-flex",
          }}
        >
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </button>
        <span style={{ fontWeight: 600, fontSize: 13, fontStyle: "italic" }}>
          {row.label}
        </span>
        {hasMore && (
          <span
            style={{
              fontSize: 11,
              padding: "1px 7px",
              borderRadius: 999,
              background: "color-mix(in srgb, var(--accent) 14%, transparent)",
              color: "var(--accent)",
              fontWeight: 600,
            }}
          >
            {row.hitCount} hits
          </span>
        )}
        <div style={{ marginLeft: "auto", display: "flex", gap: 14, fontSize: 12 }}>
          <span style={{ color: identityColor(row.bestIdentity) }}>
            {formatPercent(row.bestIdentity)} id
          </span>
          <span className="muted">E = {formatEvalue(row.bestEvalue)}</span>
          <span className="muted">{row.bestBitscore?.toFixed(0) ?? "—"} bits</span>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            style={{ padding: "0 8px", fontSize: 11 }}
            onClick={() => onHitActivate(row.bestHit)}
          >
            Best hit →
          </button>
        </div>
      </div>

      {open && hasMore && (
        <div style={{ marginTop: 8, paddingLeft: 24, display: "flex", flexDirection: "column", gap: 3 }}>
          {row.members.map((member, index) => (
            <button
              key={`${member.sseqid}-${index}`}
              type="button"
              onClick={() => onHitActivate(member)}
              style={{
                display: "flex",
                justifyContent: "space-between",
                gap: 12,
                background: "none",
                border: "none",
                padding: "2px 0",
                cursor: "pointer",
                color: "var(--text-muted)",
                fontSize: 12,
                textAlign: "left",
              }}
            >
              <code className="code-val" style={{ wordBreak: "break-all" }}>
                {member.sseqid}
              </code>
              <span style={{ whiteSpace: "nowrap" }}>
                {formatPercent(member.pident)} · E {formatEvalue(member.evalue)}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
