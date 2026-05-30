import { useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  ExternalLink,
  Download,
  Share2,
  ChevronLeft,
  ChevronUp,
  ChevronDown,
} from "lucide-react";

import type { BlastHit } from "@/api/endpoints";
import { TaxonomyDetailModal } from "@/components/taxonomy/TaxonomyDetailModal";
import { Tooltip } from "@/components/Tooltip";
import {
  extractCanonicalAccession,
  formatDecimal,
  formatEvalue,
  formatInteger,
  formatPercent,
  formatRange,
  identityColor,
  isNcbiAccessionLike,
  ncbiNuccoreUrl,
  ncbiSearchUrl,
  numberValue,
  organismFromStitle,
  parseLeadingTaxid,
  taxidLabel,
} from "./helpers";
import { ReviewBadgePopover } from "./ReviewBadgePopover";
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

  const [activeTaxon, setActiveTaxon] = useState<{
    name: string;
    taxid: number | null;
    source: "sscinames" | "stitle";
  } | null>(null);

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
              <th style={{ textAlign: "left" }}>
                Review
                <Tooltip
                  width={340}
                  content={
                    <>
                      <strong>Review classification</strong>
                      <div style={{ marginTop: 6 }}>
                        A per-HSP quality tier (Strong / Review / Low / Weak /
                        Unknown) computed from <code>% identity</code>,{" "}
                        <code>HSP cover</code>, and <code>E-value</code>. Hover
                        the badge in any row for the exact thresholds and how
                        that row matched them.
                      </div>
                    </>
                  }
                />
              </th>
              <th style={{ textAlign: "left" }}>Accession</th>
              <th style={{ textAlign: "left" }}>Description</th>
              <th style={{ textAlign: "left" }}>Scientific Name</th>
              <SortableHeader
                column="qcovs"
                label="HSP Cover"
                align="right"
                applied={applied}
                onSort={handleHeaderSort}
                hint={
                  <>
                    <strong>HSP query coverage</strong>
                    <div style={{ marginTop: 6 }}>
                      Percent of the query covered by{" "}
                      <em>this single HSP</em> only — computed from{" "}
                      <code>qstart / qend / qlen</code>.
                    </div>
                    <div className="tt-note">
                      Not the same as NCBI Web BLAST's{" "}
                      <em>Query Cover</em> column, which is the union of all
                      HSPs per subject. Use the Alignments tab for the
                      per-subject view.
                    </div>
                  </>
                }
              />
              <SortableHeader
                column="pident"
                label="% Identity"
                align="right"
                applied={applied}
                onSort={handleHeaderSort}
                hint={
                  <>
                    <strong>Percent identity</strong>
                    <div style={{ marginTop: 6 }}>
                      Fraction of identical residues across the aligned HSP.
                    </div>
                    <div className="tt-note">
                      Cell color: green when ≥ 90, amber when ≥ 70, red below 70.
                    </div>
                  </>
                }
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
                hint={
                  <>
                    <strong>Expect value</strong>
                    <div style={{ marginTop: 6 }}>
                      Expected number of chance hits with this score against a
                      database of this size. Smaller means stronger.
                    </div>
                    <div className="tt-note">
                      ≤ 1e-20 is essentially certain; ≤ 1e-5 is the usual
                      significance cutoff.
                    </div>
                  </>
                }
              />
              <SortableHeader
                column="bitscore"
                label="Max / Total"
                align="right"
                applied={applied}
                onSort={handleHeaderSort}
                hint={
                  <>
                    <strong>Max / Total bit score</strong>
                    <div style={{ marginTop: 6 }}>
                      <code>Max</code> is the bit score of this single HSP.{" "}
                      <code>Total</code> is the sum of bit scores across every
                      HSP that aligns this subject to this query in the filtered
                      result set.
                    </div>
                    <div className="tt-note">
                      Max and Total are equal when there is only one HSP per
                      subject.
                    </div>
                  </>
                }
              />
              <th style={{ textAlign: "right" }}>Query Range</th>
            </tr>
          </thead>
          <tbody>
            {hits.map((hit) => {
              const key = hitKey(hit);
              const checked = selectedHits.has(key);
              const aggregate = subjectAggregates.get(hit.sseqid);
              return (
                <tr
                  key={key}
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
                    <ReviewBadgePopover hit={hit} />
                  </td>
                  <td
                    style={{
                      fontFamily: "var(--font-mono, monospace)",
                      maxWidth: 150,
                    }}
                  >
                    {(() => {
                      const sstart = numberValue(hit.sstart);
                      const send = numberValue(hit.send);
                      let highlightStart: number | null = null;
                      let highlightStop: number | null = null;
                      if (sstart != null && send != null) {
                        highlightStart = Math.min(sstart, send);
                        highlightStop = Math.max(sstart, send);
                      }
                      const canonical = extractCanonicalAccession(hit.sseqid);
                      // Gate the internal SequenceDetail link on the same
                      // accession pattern the backend uses. Non-accession
                      // sseqids (e.g. `Query_1`, custom DB IDs) fall back to
                      // the external NCBI search link instead of producing a
                      // 422 from /api/ncbi/nuccore.
                      const looksLikeAccession = isNcbiAccessionLike(hit.sseqid);
                      const search = new URLSearchParams();
                      if (highlightStart != null && highlightStop != null) {
                        search.set("hl_start", String(highlightStart));
                        search.set("hl_stop", String(highlightStop));
                      }
                      const internalHref = `/sequence/${encodeURIComponent(canonical)}${search.toString() ? `?${search.toString()}` : ""}`;
                      return (
                        <span
                          style={{
                            display: "inline-flex",
                            gap: 6,
                            alignItems: "center",
                            maxWidth: "100%",
                            minWidth: 0,
                          }}
                        >
                          {looksLikeAccession ? (
                            <>
                              <Link
                                to={internalHref}
                                style={{
                                  color: "var(--accent)",
                                  textDecoration: "none",
                                  overflow: "hidden",
                                  textOverflow: "ellipsis",
                                  whiteSpace: "nowrap",
                                  minWidth: 0,
                                }}
                                title={`Open ${hit.sseqid} in dashboard sequence viewer`}
                              >
                                {hit.sseqid}
                              </Link>
                              <a
                                href={ncbiNuccoreUrl(hit.sseqid)}
                                target="_blank"
                                rel="noopener noreferrer"
                                style={{
                                  color: "var(--text-muted)",
                                  lineHeight: 0,
                                  flexShrink: 0,
                                }}
                                title="Open in NCBI nuccore (external)"
                                aria-label={`Open ${hit.sseqid} in NCBI (external)`}
                              >
                                <ExternalLink size={11} strokeWidth={1.5} />
                              </a>
                            </>
                          ) : (
                            <>
                              <span
                                style={{
                                  color: "var(--text)",
                                  overflow: "hidden",
                                  textOverflow: "ellipsis",
                                  whiteSpace: "nowrap",
                                  minWidth: 0,
                                }}
                                title={`${hit.sseqid} — non-accession identifier; in-app viewer is unavailable`}
                              >
                                {hit.sseqid}
                              </span>
                              <a
                                href={ncbiSearchUrl(hit.sseqid)}
                                target="_blank"
                                rel="noopener noreferrer"
                                style={{
                                  color: "var(--text-muted)",
                                  lineHeight: 0,
                                  flexShrink: 0,
                                }}
                                title="Search this identifier on NCBI (external)"
                                aria-label={`Search ${hit.sseqid} on NCBI (external)`}
                              >
                                <ExternalLink size={11} strokeWidth={1.5} />
                              </a>
                            </>
                          )}
                        </span>
                      );
                    })()}
                  </td>
                  <td style={{ maxWidth: 280, color: "var(--text-muted)" }}>
                    {hit.stitle || "—"}
                  </td>
                  <td style={{ maxWidth: 180, color: "var(--text-muted)" }}>
                    <ScientificNameCell
                      hit={hit}
                      onOpen={(payload) => setActiveTaxon(payload)}
                    />
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
                    {(hit.qframe !== undefined || hit.sframe !== undefined) && (
                      <span
                        className="muted"
                        title={
                          [
                            hit.qframe !== undefined
                              ? `Query frame ${hit.qframe}`
                              : null,
                            hit.sframe !== undefined
                              ? `Subject frame ${hit.sframe}`
                              : null,
                          ]
                            .filter(Boolean)
                            .join(" · ") ||
                          "Reading frame for translated BLAST programs"
                        }
                        style={{ marginLeft: 6, fontSize: 11 }}
                      >
                        ({hit.qframe ?? "·"}/{hit.sframe ?? "·"})
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {activeTaxon && (
        <TaxonomyDetailModal
          open
          scientificName={activeTaxon.name}
          taxid={activeTaxon.taxid}
          organismSource={activeTaxon.source}
          onClose={() => setActiveTaxon(null)}
        />
      )}
    </div>
  );
}

interface ScientificNameCellProps {
  hit: BlastHit;
  onOpen: (payload: {
    name: string;
    taxid: number | null;
    source: "sscinames" | "stitle";
  }) => void;
}

function ScientificNameCell({ hit, onOpen }: ScientificNameCellProps) {
  // Source preference matches the cell's previous fallback chain:
  // sscinames (trusted) → organism parsed from stitle (heuristic).
  // The taxid label is shown as plain text only when nothing else is
  // available because the bare number isn't useful to open the modal.
  const stitleOrganism = organismFromStitle(hit.stitle);
  const trustedName = hit.sscinames?.trim() || "";
  const heuristicName = !trustedName ? stitleOrganism.trim() : "";
  const displayName = trustedName || heuristicName;
  const fallbackText = taxidLabel(hit.staxids);

  if (!displayName) {
    return <>{fallbackText || "—"}</>;
  }

  const taxid = parseLeadingTaxid(hit.staxids);
  const source: "sscinames" | "stitle" = trustedName ? "sscinames" : "stitle";

  return (
    <button
      type="button"
      onClick={() =>
        onOpen({ name: displayName, taxid, source })
      }
      title="Open NCBI taxonomy details"
      style={{
        background: "transparent",
        border: 0,
        padding: 0,
        cursor: "pointer",
        color: "var(--accent)",
        fontFamily: "inherit",
        fontSize: "inherit",
        textAlign: "left",
        textDecoration: "underline dotted",
        textUnderlineOffset: 3,
      }}
    >
      {displayName}
    </button>
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
  /**
   * Optional rich tooltip rendered next to the column label. Use this
   * to explain non-obvious BLAST terminology (HSP cover vs query
   * cover, E-value scale, etc.). The (?) icon does not steal sort
   * clicks because it is its own `<button>`.
   */
  hint?: ReactNode;
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
  hint,
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
        {hint && (
          <span
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => event.stopPropagation()}
          >
            <Tooltip content={hint} width={340} />
          </span>
        )}
      </span>
    </th>
  );
}
