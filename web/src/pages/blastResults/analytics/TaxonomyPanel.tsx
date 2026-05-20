import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, ExternalLink, Loader2 } from "lucide-react";

import { blastApi, type BlastHit, type BlastTaxonomyRow } from "@/api/endpoints";
import {
  formatEvalue,
  isPartialResult,
  ncbiTaxonomyUrl,
  numberValue,
} from "./helpers";
import { DegradedBanner } from "./DegradedBanner";
import { ResultsPendingPanel } from "./ResultsPendingPanel";
import type { BlastAnalyticsState } from "./useBlastAnalyticsState";

export interface TaxonomyPanelProps {
  analytics: BlastAnalyticsState;
  /** Identity props needed to call the server-side rollup endpoint. */
  jobId: string;
  subscriptionId: string;
  storageAccount: string;
  resourceGroup: string;
  resultsPending?: boolean;
}

type TaxonomyView = "organism" | "lineage";

interface TaxonomyRow {
  key: string;
  organism: string;
  taxid: string;
  count: number;
  bestEvalue: number | null;
  topBitscore: number | null;
  lineageEx?: Array<{ rank: string; taxid: number; scientific_name: string }>;
}

/**
 * NCBI's "Taxonomy" report with two sub-views:
 *
 *   Organism: flat table sorted by hit count (the default).
 *   Lineage : hierarchical tree built from each taxid's `lineage_ex`
 *             chain — mirrors NCBI's "Lineage" sub-tab.
 *
 * Strategy: prefer the server-side `/results/taxonomy` endpoint (full
 * scan across every shard for the filtered hit set). Fall back to a
 * page-local rollup of the alignments query when the server is
 * degraded or unavailable, so the tab is never empty in a degraded
 * deployment. Lineage fetch is opt-in (`include_lineage=true`) so the
 * default Organism view doesn't pay for N eutils round-trips.
 */
export function TaxonomyPanel({
  analytics,
  jobId,
  subscriptionId,
  storageAccount,
  resourceGroup,
  resultsPending = false,
}: TaxonomyPanelProps) {
  const { alignQuery, alignments, applied } = analytics;
  const [view, setView] = useState<TaxonomyView>("organism");

  const taxonomyQuery = useQuery({
    queryKey: [
      "blast-taxonomy",
      jobId,
      subscriptionId,
      storageAccount,
      resourceGroup,
      applied,
      view,
    ],
    queryFn: () =>
      blastApi.resultsTaxonomy(jobId, subscriptionId, storageAccount, resourceGroup, {
        query_id: applied.queryFilter || undefined,
        subject_id: applied.subjectFilter || undefined,
        organism: applied.organismFilter || undefined,
        min_identity: applied.minIdentity > 0 ? applied.minIdentity : undefined,
        min_query_cover:
          applied.minQueryCover > 0 ? applied.minQueryCover : undefined,
        max_evalue: applied.maxEvalue,
        include_lineage: view === "lineage",
      }),
    enabled: Boolean(jobId && subscriptionId && storageAccount && !resultsPending),
    staleTime: 60_000,
  });

  const serverDegraded = Boolean(taxonomyQuery.data?.degraded);
  const serverRows: TaxonomyRow[] = useMemo(() => {
    const data = taxonomyQuery.data;
    if (!data || serverDegraded) return [];
    return data.organisms.map((row: BlastTaxonomyRow) => ({
      key: row.key,
      organism: row.organism,
      taxid: row.taxid,
      count: row.count,
      bestEvalue: row.best_evalue,
      topBitscore: row.top_bitscore,
      lineageEx: row.lineage_ex,
    }));
  }, [taxonomyQuery.data, serverDegraded]);

  const fallbackRows = useMemo(() => rollupByOrganism(alignments), [alignments]);

  if (resultsPending) {
    return <ResultsPendingPanel />;
  }

  const usingFallback = serverRows.length === 0 && fallbackRows.length > 0;
  const rows = serverRows.length > 0 ? serverRows : fallbackRows;
  const sourceTotalHits =
    !usingFallback && taxonomyQuery.data
      ? (taxonomyQuery.data.filtered_hits ?? taxonomyQuery.data.total_hits ?? 0)
      : alignments.length;

  if (taxonomyQuery.isLoading && alignQuery.isLoading) {
    return (
      <div className="glass-card" style={{ padding: 40, textAlign: "center" }}>
        <Loader2 size={20} className="spin" style={{ color: "var(--accent)" }} />
        <p className="muted" style={{ marginTop: 8 }}>
          Building taxonomy rollup...
        </p>
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <div className="glass-card" style={{ padding: 24, textAlign: "center" }}>
        <p className="muted">
          No organism metadata in the current hit set. Re-run with{" "}
          <code>-outfmt</code> options that include <code>sscinames</code> or{" "}
          <code>staxids</code> to enable this view.
        </p>
      </div>
    );
  }

  const topHitsPerOrg = rows.reduce(
    (max, row) => (row.count > max ? row.count : max),
    0,
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {serverDegraded && taxonomyQuery.data && (
        <DegradedBanner data={taxonomyQuery.data} />
      )}
      {!serverDegraded &&
        taxonomyQuery.data &&
        isPartialResult(taxonomyQuery.data) && (
          <DegradedBanner data={taxonomyQuery.data} />
        )}

      <div className="glass-card" style={{ padding: 16 }}>
        <div
          style={{
            display: "flex",
            alignItems: "baseline",
            justifyContent: "space-between",
            marginBottom: 12,
            flexWrap: "wrap",
            gap: 8,
          }}
        >
          <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
            <h3 style={{ margin: 0, fontSize: 14 }}>Taxonomy</h3>
            <TaxonomySubTabs
              view={view}
              onChange={setView}
              lineageDisabled={usingFallback}
            />
          </div>
          <span className="muted" style={{ fontSize: 12 }}>
            {rows.length} organism{rows.length === 1 ? "" : "s"} ·{" "}
            {sourceTotalHits.toLocaleString()} hit
            {sourceTotalHits === 1 ? "" : "s"}
            {usingFallback ? " (visible page only)" : " (full result set)"}
            {view === "lineage" && taxonomyQuery.data?.lineage && (
              <>
                {" · "}
                lineage fetched for {taxonomyQuery.data.lineage.looked_up} taxa
                {taxonomyQuery.data.lineage.failed
                  ? `, ${taxonomyQuery.data.lineage.failed} failed`
                  : ""}
              </>
            )}
          </span>
        </div>

        {view === "organism" ? (
          <OrganismTable rows={rows} topHitsPerOrg={topHitsPerOrg} />
        ) : (
          <LineageTree rows={rows} loading={taxonomyQuery.isFetching} />
        )}

        <p className="muted" style={{ fontSize: 11, marginTop: 8 }}>
          {usingFallback
            ? "Server-side taxonomy was unavailable — falling back to a rollup of the alignments visible on this page. Increase Show or apply Reset to widen the view."
            : view === "lineage"
              ? "Lineage chain is fetched from NCBI eutils for the top 20 organisms by hit count (cached server-side). Switch to Organism for the flat list."
              : "Rollup runs server-side across the full filtered result set (capped at 2,000 organisms). Narrow with the filter bar to focus on a clade."}
        </p>
      </div>
    </div>
  );
}

interface TaxonomySubTabsProps {
  view: TaxonomyView;
  onChange: (view: TaxonomyView) => void;
  lineageDisabled: boolean;
}

function TaxonomySubTabs({ view, onChange, lineageDisabled }: TaxonomySubTabsProps) {
  const TABS: Array<{ key: TaxonomyView; label: string; title?: string }> = [
    { key: "organism", label: "Organism", title: "Flat list of organisms" },
    {
      key: "lineage",
      label: "Lineage",
      title: lineageDisabled
        ? "Lineage view requires the server-side taxonomy endpoint"
        : "Hierarchical tree built from NCBI lineage chains",
    },
  ];
  return (
    <span
      style={{ display: "inline-flex", gap: 2 }}
      role="tablist"
      aria-label="Taxonomy view"
    >
      {TABS.map((tab) => {
        const isActive = tab.key === view;
        const disabled = lineageDisabled && tab.key === "lineage";
        return (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={isActive}
            disabled={disabled}
            title={tab.title}
            onClick={() => onChange(tab.key)}
            style={{
              padding: "4px 10px",
              fontSize: 12,
              borderRadius: 4,
              border: 0,
              background: isActive ? "var(--accent)" : "transparent",
              color: isActive ? "var(--text-on-accent, #fff)" : "var(--text-muted)",
              cursor: disabled ? "not-allowed" : "pointer",
              opacity: disabled ? 0.4 : 1,
            }}
          >
            {tab.label}
          </button>
        );
      })}
    </span>
  );
}

interface OrganismTableProps {
  rows: TaxonomyRow[];
  topHitsPerOrg: number;
}

function OrganismTable({ rows, topHitsPerOrg }: OrganismTableProps) {
  return (
    <div style={{ overflowX: "auto" }}>
      <table className="table" style={{ width: "100%", fontSize: 13 }}>
        <thead>
          <tr>
            <th style={{ textAlign: "left" }}>Organism</th>
            <th style={{ textAlign: "left" }}>Taxid</th>
            <th style={{ textAlign: "right" }}>Hits</th>
            <th style={{ textAlign: "right" }}>Best E-value</th>
            <th style={{ textAlign: "right" }}>Top bit score</th>
            <th style={{ width: 220 }}>Distribution</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key}>
              <td style={{ fontWeight: 500 }}>
                {row.organism ||
                  (row.taxid ? `taxid:${row.taxid}` : "Unclassified")}
              </td>
              <td>
                {row.taxid ? (
                  <a
                    href={ncbiTaxonomyUrl(row.taxid)}
                    target="_blank"
                    rel="noopener noreferrer"
                    style={{
                      color: "var(--accent)",
                      display: "inline-flex",
                      gap: 4,
                      alignItems: "center",
                    }}
                    title="Open in NCBI Taxonomy Browser"
                  >
                    {row.taxid}
                    <ExternalLink size={11} strokeWidth={1.5} />
                  </a>
                ) : (
                  <span className="muted">—</span>
                )}
              </td>
              <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                {row.count.toLocaleString()}
              </td>
              <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                {formatEvalue(row.bestEvalue)}
              </td>
              <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                {row.topBitscore !== null ? row.topBitscore.toFixed(1) : "—"}
              </td>
              <td>
                <div
                  style={{
                    width: "100%",
                    height: 10,
                    background: "var(--glass-bg)",
                    borderRadius: 3,
                    overflow: "hidden",
                  }}
                >
                  <div
                    style={{
                      width: `${topHitsPerOrg > 0 ? (row.count / topHitsPerOrg) * 100 : 0}%`,
                      height: "100%",
                      background: "var(--accent)",
                      borderRadius: 3,
                    }}
                  />
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface LineageTreeProps {
  rows: TaxonomyRow[];
  loading: boolean;
}

interface LineageNode {
  taxid: string;
  name: string;
  rank: string;
  totalCount: number;
  leafCount: number;
  children: Map<string, LineageNode>;
}

/**
 * Hierarchical "Lineage" view, NCBI-style. Builds a single tree from
 * every row's `lineageEx` chain (root → leaf) and sums hit counts up
 * the tree. Rows without `lineageEx` fall under a synthetic
 * "Unresolved" bucket so the user knows they were dropped.
 */
function LineageTree({ rows, loading }: LineageTreeProps) {
  const tree = useMemo(() => buildLineageTree(rows), [rows]);

  if (loading && tree.children.size === 0) {
    return (
      <div style={{ padding: 24, textAlign: "center" }}>
        <Loader2 size={20} className="spin" style={{ color: "var(--accent)" }} />
        <p className="muted" style={{ marginTop: 8 }}>
          Fetching NCBI lineage chains...
        </p>
      </div>
    );
  }

  if (tree.children.size === 0) {
    return (
      <p className="muted" style={{ fontSize: 13 }}>
        No lineage data available — NCBI eutils may be unreachable, or none of
        the organisms in this hit set carry a taxid.
      </p>
    );
  }

  return (
    <ul
      style={{
        listStyle: "none",
        margin: 0,
        padding: 0,
        fontSize: 13,
      }}
      role="tree"
      aria-label="Taxonomic lineage tree"
    >
      {[...tree.children.values()]
        .sort((a, b) => b.totalCount - a.totalCount)
        .map((node) => (
          <LineageNodeView key={`${node.taxid}:${node.name}`} node={node} depth={0} />
        ))}
    </ul>
  );
}

function LineageNodeView({ node, depth }: { node: LineageNode; depth: number }) {
  // Auto-expand the top two depths so users see the shape of the tree
  // without having to click every node. Deeper nodes default collapsed.
  const [expanded, setExpanded] = useState(depth < 2);
  const hasChildren = node.children.size > 0;
  const indent = depth * 16;
  return (
    <li role="treeitem" aria-expanded={hasChildren ? expanded : undefined}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "2px 4px",
          paddingLeft: indent + 4,
          borderRadius: 4,
        }}
      >
        {hasChildren ? (
          <button
            type="button"
            onClick={() => setExpanded((value) => !value)}
            aria-label={expanded ? `Collapse ${node.name}` : `Expand ${node.name}`}
            style={{
              background: "transparent",
              border: 0,
              padding: 0,
              cursor: "pointer",
              color: "var(--text-muted)",
              transition: "transform 150ms ease-out",
              transform: expanded ? "rotate(90deg)" : "rotate(0deg)",
              display: "inline-flex",
            }}
          >
            <ChevronRight size={12} strokeWidth={2} />
          </button>
        ) : (
          <span style={{ width: 12, display: "inline-block" }} />
        )}
        <span
          className="muted"
          style={{
            fontSize: 11,
            minWidth: 64,
            fontFamily: "var(--font-mono, monospace)",
          }}
        >
          {node.rank}
        </span>
        <span style={{ fontWeight: depth === 0 ? 600 : 500 }}>{node.name}</span>
        {node.taxid && (
          <a
            href={ncbiTaxonomyUrl(node.taxid)}
            target="_blank"
            rel="noopener noreferrer"
            style={{
              color: "var(--text-muted)",
              display: "inline-flex",
              marginLeft: 2,
            }}
            title={`Open ${node.name} in NCBI Taxonomy Browser`}
          >
            <ExternalLink size={10} strokeWidth={1.5} />
          </a>
        )}
        <span
          className="muted"
          style={{ fontSize: 11, marginLeft: "auto", fontVariantNumeric: "tabular-nums" }}
        >
          {node.totalCount.toLocaleString()} hit
          {node.totalCount === 1 ? "" : "s"}
          {node.leafCount > 0 && node.leafCount !== node.totalCount && (
            <span style={{ marginLeft: 6 }}>
              ({node.leafCount.toLocaleString()} at this rank)
            </span>
          )}
        </span>
      </div>
      {hasChildren && expanded && (
        <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {[...node.children.values()]
            .sort((a, b) => b.totalCount - a.totalCount)
            .map((child) => (
              <LineageNodeView
                key={`${child.taxid}:${child.name}`}
                node={child}
                depth={depth + 1}
              />
            ))}
        </ul>
      )}
    </li>
  );
}

function buildLineageTree(rows: TaxonomyRow[]): LineageNode {
  const root: LineageNode = {
    taxid: "",
    name: "(root)",
    rank: "root",
    totalCount: 0,
    leafCount: 0,
    children: new Map(),
  };
  let unresolvedBucket: LineageNode | null = null;

  for (const row of rows) {
    if (!row.lineageEx || row.lineageEx.length === 0) {
      // Group all unresolved rows under one synthetic bucket so users
      // know how much of the result set is missing from the tree.
      if (!unresolvedBucket) {
        unresolvedBucket = {
          taxid: "",
          name: "Unresolved (no lineage fetched)",
          rank: "—",
          totalCount: 0,
          leafCount: 0,
          children: new Map(),
        };
        root.children.set("unresolved", unresolvedBucket);
      }
      unresolvedBucket.totalCount += row.count;
      unresolvedBucket.leafCount += row.count;
      const leafKey = row.organism || row.taxid || "?";
      const existing = unresolvedBucket.children.get(leafKey);
      if (existing) {
        existing.totalCount += row.count;
        existing.leafCount += row.count;
      } else {
        unresolvedBucket.children.set(leafKey, {
          taxid: row.taxid,
          name: row.organism || `taxid:${row.taxid}`,
          rank: "species",
          totalCount: row.count,
          leafCount: row.count,
          children: new Map(),
        });
      }
      continue;
    }
    // Walk root → leaf, creating nodes as needed. Sum totalCount up the
    // chain so an inner node shows the combined hits of every descendant.
    let cursor = root;
    for (const step of row.lineageEx) {
      const key = `${step.taxid}`;
      const existing = cursor.children.get(key);
      if (existing) {
        existing.totalCount += row.count;
        cursor = existing;
      } else {
        const newNode: LineageNode = {
          taxid: String(step.taxid),
          name: step.scientific_name || `taxid:${step.taxid}`,
          rank: step.rank || "no rank",
          totalCount: row.count,
          leafCount: 0,
          children: new Map(),
        };
        cursor.children.set(key, newNode);
        cursor = newNode;
      }
    }
    // Add the leaf organism node itself.
    const leafKey = `leaf:${row.taxid || row.organism}`;
    const leafExisting = cursor.children.get(leafKey);
    if (leafExisting) {
      leafExisting.totalCount += row.count;
      leafExisting.leafCount += row.count;
    } else {
      cursor.children.set(leafKey, {
        taxid: row.taxid,
        name: row.organism || `taxid:${row.taxid}`,
        rank: "species",
        totalCount: row.count,
        leafCount: row.count,
        children: new Map(),
      });
    }
    // Bubble leafCount up so inner nodes can report "X at this rank".
    // (We track total separately above.)
  }
  return root;
}

function rollupByOrganism(hits: BlastHit[]): TaxonomyRow[] {
  const map = new Map<string, TaxonomyRow>();
  for (const hit of hits) {
    const organism = (hit.sscinames || "").split(";")[0]?.trim() ?? "";
    const taxid = (hit.staxids || "").split(";")[0]?.trim() ?? "";
    const key = (organism || taxid || "unclassified").toLowerCase();
    const evalue = numberValue(hit.evalue);
    const bitscore = numberValue(hit.bitscore);
    const existing = map.get(key);
    if (existing) {
      existing.count += 1;
      if (
        evalue !== null &&
        (existing.bestEvalue === null || evalue < existing.bestEvalue)
      ) {
        existing.bestEvalue = evalue;
      }
      if (
        bitscore !== null &&
        (existing.topBitscore === null || bitscore > existing.topBitscore)
      ) {
        existing.topBitscore = bitscore;
      }
    } else {
      map.set(key, {
        key,
        organism,
        taxid,
        count: 1,
        bestEvalue: evalue,
        topBitscore: bitscore,
      });
    }
  }
  return [...map.values()].sort((a, b) => b.count - a.count);
}

/** Exported so `buildLineageTree` can be unit-tested without React. */
export const __internals = { buildLineageTree };
