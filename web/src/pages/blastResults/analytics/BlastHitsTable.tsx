import { useMemo } from "react";
import {
  ExternalLink,
  Download,
  Share2,
  ChevronLeft,
  ChevronUp,
  ChevronDown,
} from "lucide-react";

import type { BlastHit } from "@/api/endpoints";
import {
  formatDecimal,
  formatEvalue,
  formatInteger,
  formatPercent,
  formatRange,
  identityColor,
  ncbiNuccoreUrl,
  numberValue,
  organismFromStitle,
  taxidLabel,
} from "./helpers";
import {
  hitKey,
  type BlastAnalyticsState,
  type HitSortBy,
} from "./useBlastAnalyticsState";

export interface BlastHitsTableProps {
  hits: BlastHit[];
  analytics: BlastAnalyticsState;
  onSendToMsa?: (selectedHits: BlastHit[]) => void;
  onDownloadSelection?: (selectedHits: BlastHit[]) => void;
  /**
   * Optional handler invoked when the user clicks the multi-HSP
   * `Max / Total (N HSPs)` indicator. The Descriptions tab uses it to
   * deep-link into the Alignments tab with the subject already
   * narrowed.
   */
  onSubjectDrilldown?: (hit: BlastHit) => void;
}

/**
 * The "Descriptions" table — equivalent to NCBI's
 * "Sequences producing significant alignments" panel. Adds bulk
 * selection, per-row deep links to NCBI nuccore + Graphics, and
 * groups the column meaning the same way NCBI does (Review badge,
 * Accession, Description, Scientific Name, HSP Cover, % Identity,
 * Length, E-value, Bit Score, Range). The originating query is
 * already selectable in the filter bar and visible in the Alignments
 * tab, so it is not duplicated as a column here; the source shard is
 * an internal artefact and is hidden from the table.
 */
export function BlastHitsTable({
  hits,
  analytics,
  onSendToMsa,
  onDownloadSelection,
  onSubjectDrilldown,
}: BlastHitsTableProps) {
  const { selectedHits, toggleHit, setSelectionFromKeys, clearSelection, applied, applyImmediate } =
    analytics;

  const allKeys = hits.map((hit) => hitKey(hit));
  const allSelected =
    allKeys.length > 0 && allKeys.every((key) => selectedHits.has(key));
  const someSelected =
    allKeys.some((key) => selectedHits.has(key)) && !allSelected;

  const selectedRows = hits.filter((hit) => selectedHits.has(hitKey(hit)));

  // Per-subject rollup so the table can show NCBI's "Max Score / Total Score"
  // pair plus the HSP count. Prefer the backend aggregate (spans the
  // whole filtered result set, not just the visible page); fall back to
  // a page-local rollup if the server didn't return one (older builds
  // or degraded responses).
  const serverAggregates = analytics.alignQuery.data?.subject_aggregates;
  const subjectAggregates = useMemo(() => {
    if (serverAggregates && serverAggregates.length > 0) {
      const map = new Map<string, SubjectAggregate>();
      for (const row of serverAggregates) {
        map.set(row.sseqid, {
          maxBitscore: row.max_bitscore,
          totalBitscore: row.total_bitscore,
          hspCount: row.hsp_count,
        });
      }
      return map;
    }
    return buildSubjectAggregates(hits);
  }, [serverAggregates, hits]);

  const handleHeaderSort = (column: HitSortBy) => {
    if (applied.sortBy === column) {
      applyImmediate({ sortDir: applied.sortDir === "asc" ? "desc" : "asc" });
    } else {
      // Most BLAST metrics (E-value, length) sort ascending by default
      // when first clicked; bitscore / identity / cover sort descending
      // (high values first). Match the cognitive default.
      const ascDefault: HitSortBy[] = ["evalue", "length"];
      applyImmediate({
        sortBy: column,
        sortDir: ascDefault.includes(column) ? "asc" : "desc",
      });
    }
  };

  return (
    <div className="glass-card" style={{ padding: 0, overflow: "hidden" }}>
      {selectedHits.size > 0 && (
        <SelectionActionBar
          selectedCount={selectedHits.size}
          onClear={clearSelection}
          onDownload={
            onDownloadSelection ? () => onDownloadSelection(selectedRows) : undefined
          }
          onSendToMsa={
            onSendToMsa ? () => onSendToMsa(selectedRows) : undefined
          }
        />
      )}
      <div style={{ padding: 16, overflowX: "auto" }}>
        <table className="table" style={{ width: "100%", minWidth: 1320, fontSize: 13 }}>
          <thead>
            <tr>
              <th style={{ width: 28, textAlign: "left" }}>
                <input
                  type="checkbox"
                  checked={allSelected}
                  ref={(node) => {
                    if (node) node.indeterminate = someSelected;
                  }}
                  onChange={(event) => {
                    if (event.target.checked) setSelectionFromKeys(allKeys);
                    else clearSelection();
                  }}
                  title={allSelected ? "Deselect all" : "Select all on this page"}
                />
              </th>
              <th style={{ textAlign: "left" }}>Review</th>
              <th style={{ textAlign: "left" }}>Accession</th>
              <th style={{ textAlign: "left" }}>Description</th>
              <th style={{ textAlign: "left" }}>Scientific Name</th>
              <SortableHeader
                column="qcovs"
                label="HSP Cover"
                align="right"
                applied={applied}
                onSort={handleHeaderSort}
              />
              <SortableHeader
                column="pident"
                label="% Identity"
                align="right"
                applied={applied}
                onSort={handleHeaderSort}
              />
              <SortableHeader
                column="length"
                label="Length"
                align="right"
                applied={applied}
                onSort={handleHeaderSort}
              />
              <SortableHeader
                column="evalue"
                label="E-value"
                align="right"
                applied={applied}
                onSort={handleHeaderSort}
              />
              <SortableHeader
                column="bitscore"
                label="Max / Total"
                align="right"
                applied={applied}
                onSort={handleHeaderSort}
                title="Max bit score on this HSP / total bit score summed across every HSP for this subject on the visible page"
              />
              <th style={{ textAlign: "right" }}>Query Range</th>
              <th style={{ textAlign: "left" }}>Shard</th>
            </tr>
          </thead>
          <tbody>
            {hits.map((hit, index) => {
              const key = hitKey(hit);
              const checked = selectedHits.has(key);
              const aggregate = subjectAggregates.get(hit.sseqid);
              return (
                <tr
                  key={`${key}-${index}`}
                  style={{
                    background: checked
                      ? "color-mix(in srgb, var(--accent) 8%, transparent)"
                      : undefined,
                  }}
                >
                  <td>
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleHit(key)}
                      aria-label={`Select hit ${hit.qseqid} → ${hit.sseqid}`}
                    />
                  </td>
                  <td>
                    <ReviewBadge hit={hit} />
                  </td>
                  <td
                    style={{
                      fontFamily: "var(--font-mono, monospace)",
                      maxWidth: 150,
                    }}
                  >
                    <a
                      href={ncbiNuccoreUrl(hit.sseqid)}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{
                        color: "var(--accent)",
                        display: "inline-flex",
                        gap: 4,
                      }}
                      title="Open in NCBI nuccore"
                    >
                      {hit.sseqid}
                      <ExternalLink size={12} strokeWidth={1.5} />
                    </a>
                  </td>
                  <td style={{ maxWidth: 280, color: "var(--text-muted)" }}>
                    {hit.stitle || "—"}
                  </td>
                  <td style={{ maxWidth: 180, color: "var(--text-muted)" }}>
                    {hit.sscinames ||
                      taxidLabel(hit.staxids) ||
                      organismFromStitle(hit.stitle) ||
                      "—"}
                  </td>
                  <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                    {formatPercent(hit.qcovs)}
                  </td>
                  <td
                    style={{
                      textAlign: "right",
                      color: identityColor(hit.pident),
                      fontVariantNumeric: "tabular-nums",
                    }}
                  >
                    {formatDecimal(hit.pident, 1)}
                  </td>
                  <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                    {formatInteger(hit.length)}
                  </td>
                  <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                    {formatEvalue(hit.evalue)}
                  </td>
                  <td
                    style={{
                      textAlign: "right",
                      fontVariantNumeric: "tabular-nums",
                    }}
                    title={
                      aggregate && aggregate.hspCount > 1
                        ? `Max bit on this HSP / sum of ${aggregate.hspCount} HSPs for ${hit.sseqid}`
                        : undefined
                    }
                  >
                    {formatDecimal(hit.bitscore, 1)}
                    {aggregate && aggregate.hspCount > 1 && (
                      <>
                        <span className="muted" style={{ margin: "0 4px" }}>/</span>
                        {onSubjectDrilldown ? (
                          <button
                            type="button"
                            onClick={() => onSubjectDrilldown(hit)}
                            title={`Show all ${aggregate.hspCount} HSPs for ${hit.sseqid} in the Alignments tab`}
                            aria-label={`Open Alignments tab narrowed to ${aggregate.hspCount} HSPs for ${hit.sseqid}`}
                            style={{
                              background: "transparent",
                              border: 0,
                              padding: 0,
                              cursor: "pointer",
                              color: "var(--accent)",
                              fontWeight: 700,
                              fontFamily: "inherit",
                              fontSize: "inherit",
                              textDecoration: "underline dotted",
                              textUnderlineOffset: 3,
                            }}
                          >
                            {aggregate.totalBitscore.toFixed(1)}
                            <span
                              className="muted"
                              style={{
                                marginLeft: 4,
                                fontSize: 11,
                                fontWeight: 400,
                              }}
                            >
                              ({aggregate.hspCount} HSPs)
                            </span>
                          </button>
                        ) : (
                          <>
                            <strong style={{ color: "var(--accent)" }}>
                              {aggregate.totalBitscore.toFixed(1)}
                            </strong>
                            <span
                              className="muted"
                              style={{ marginLeft: 4, fontSize: 11 }}
                            >
                              ({aggregate.hspCount} HSPs)
                            </span>
                          </>
                        )}
                      </>
                    )}
                  </td>
                  <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                    {formatRange(hit.qstart, hit.qend)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

interface SelectionActionBarProps {
  selectedCount: number;
  onClear: () => void;
  onDownload?: () => void;
  onSendToMsa?: () => void;
}

function SelectionActionBar({
  selectedCount,
  onClear,
  onDownload,
  onSendToMsa,
}: SelectionActionBarProps) {
  return (
    <div
      style={{
        position: "sticky",
        top: 0,
        zIndex: 2,
        background: "color-mix(in srgb, var(--accent) 14%, var(--bg-tertiary))",
        borderBottom: "1px solid var(--glass-border)",
        padding: "8px 16px",
        display: "flex",
        alignItems: "center",
        gap: 12,
      }}
    >
      <span style={{ fontSize: 13, color: "var(--text-primary)" }}>
        <strong>{selectedCount}</strong> hit{selectedCount === 1 ? "" : "s"} selected
      </span>
      {onDownload && (
        <button
          type="button"
          className="btn btn--sm btn--primary"
          onClick={onDownload}
          style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
          aria-label={`Download a CSV of ${selectedCount} selected accession${selectedCount === 1 ? "" : "s"}`}
        >
          <Download size={13} aria-hidden="true" /> Download selection (CSV)
        </button>
      )}
      {onSendToMsa && (
        <button
          type="button"
          className="btn btn--sm btn--ghost"
          onClick={onSendToMsa}
          style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
          title="Open the selected accessions in NCBI MSA Viewer (a new tab will open)"
          aria-label={`Send ${selectedCount} selected accession${selectedCount === 1 ? "" : "s"} to NCBI MSA Viewer`}
        >
          <Share2 size={13} aria-hidden="true" /> Send to MSA Viewer
        </button>
      )}
      <span style={{ flex: 1 }} />
      <button
        type="button"
        className="btn btn--sm btn--ghost"
        onClick={onClear}
        style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
      >
        <ChevronLeft size={13} /> Clear selection
      </button>
    </div>
  );
}

interface SubjectAggregate {
  totalBitscore: number;
  maxBitscore: number;
  hspCount: number;
}

/**
 * Returns one aggregate per subject (`sseqid`) reachable on the current
 * page: total bit-score (sum across HSPs), max bit-score, and how many
 * HSPs landed on that subject. Lets the Bit Score cell render the
 * NCBI-style `max / total (N HSPs)` triple without a backend change.
 *
 * Exported as a pure helper so the `useMemo` dep can stay `[hits]` —
 * the previous version was wrapped in a "use…" name but didn't use a
 * hook, which violated React's naming convention.
 */
export function buildSubjectAggregates(
  hits: BlastHit[],
): Map<string, SubjectAggregate> {
  const map = new Map<string, SubjectAggregate>();
  for (const hit of hits) {
    const bitscore = numberValue(hit.bitscore) ?? 0;
    const existing = map.get(hit.sseqid);
    if (existing) {
      existing.totalBitscore += bitscore;
      if (bitscore > existing.maxBitscore) existing.maxBitscore = bitscore;
      existing.hspCount += 1;
    } else {
      map.set(hit.sseqid, {
        totalBitscore: bitscore,
        maxBitscore: bitscore,
        hspCount: 1,
      });
    }
  }
  return map;
}

interface SortableHeaderProps {
  column: HitSortBy;
  label: string;
  align: "left" | "right";
  applied: { sortBy: HitSortBy; sortDir: "asc" | "desc" };
  onSort: (column: HitSortBy) => void;
  title?: string;
}

/**
 * Column header that toggles the applied sort by `column` on click or
 * keyboard activation — the sort is applied *immediately* (no Apply
 * button) because researchers expect column sort to feel direct, the
 * way NCBI does it. `<th>` elements aren't natively focusable, so we
 * opt in via `tabIndex={0}` + `role="button"` + an explicit `Enter` /
 * Space handler.
 */
function SortableHeader({
  column,
  label,
  align,
  applied,
  onSort,
  title,
}: SortableHeaderProps) {
  const active = applied.sortBy === column;
  const Icon = active && applied.sortDir === "asc" ? ChevronUp : ChevronDown;
  const ariaSort: "ascending" | "descending" | "none" = active
    ? applied.sortDir === "asc"
      ? "ascending"
      : "descending"
    : "none";
  return (
    <th
      role="button"
      tabIndex={0}
      aria-sort={ariaSort}
      style={{
        textAlign: align,
        cursor: "pointer",
        userSelect: "none",
        color: active ? "var(--accent)" : undefined,
      }}
      onClick={() => onSort(column)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSort(column);
        }
      }}
      title={title ?? `Sort by ${label.toLowerCase()}`}
      aria-label={`Sort by ${label.toLowerCase()}${
        active
          ? `, currently ${applied.sortDir === "asc" ? "ascending" : "descending"}`
          : ""
      }`}
    >
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 2,
          justifyContent: align === "right" ? "flex-end" : "flex-start",
          width: "100%",
        }}
      >
        {label}
        <Icon
          size={12}
          strokeWidth={2}
          aria-hidden="true"
          style={{
            opacity: active ? 1 : 0.3,
            transition: "opacity 150ms ease-out",
          }}
        />
      </span>
    </th>
  );
}

function ReviewBadge({ hit }: { hit: BlastHit }) {
  const status = hit.review_status ?? "unclassified";
  const labelByStatus: Record<NonNullable<BlastHit["review_status"]>, string> = {
    strong_match: "Strong",
    review_priority: "Review",
    low_confidence: "Low",
    weak_hit: "Weak",
    unclassified: "Unknown",
  };
  const colorByStatus: Record<NonNullable<BlastHit["review_status"]>, string> = {
    strong_match: "var(--success)",
    review_priority: "var(--warning)",
    low_confidence: "var(--accent)",
    weak_hit: "var(--text-muted)",
    unclassified: "var(--text-muted)",
  };
  return (
    <span
      title={hit.review_reason}
      style={{
        display: "inline-flex",
        alignItems: "center",
        border: `1px solid ${colorByStatus[status]}`,
        borderRadius: 999,
        color: colorByStatus[status],
        fontSize: 11,
        fontWeight: 600,
        padding: "2px 7px",
        whiteSpace: "nowrap",
      }}
    >
      {labelByStatus[status]}
    </span>
  );
}
