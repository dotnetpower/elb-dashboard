import { useState, useMemo } from "react";
import {
  ChevronDown,
  ChevronRight,
  Loader2,
  RotateCcw,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import type { TaxonomyLineageNode } from "@/api/blast";

/* ── rank definitions ── */

const MAJOR_RANKS = new Set([
  "superkingdom",
  "kingdom",
  "phylum",
  "class",
  "order",
  "family",
  "genus",
  "species",
]);

const RANK_COLORS: Record<string, string> = {
  superkingdom: "#e07b8a",
  kingdom: "#e07b8a",
  phylum: "#f0c674",
  class: "#d0a6ff",
  order: "#7aa7ff",
  family: "#6ad6a3",
  genus: "#5cc9e0",
  species: "#e8ecf4",
};

/* ── SVG cladogram layout constants ── */

const NODE_R = 6;
const NODE_R_MINOR = 4.5;
const NODE_R_SIBLING = 4;
const ROW_H = 36;
const ROW_H_MINOR = 22;
const ROW_H_SIBLING = 20;
const INDENT = 18;
const LABEL_GAP = 12;
const LEFT_PAD = 10;
const TOP_PAD = 12;
const RANK_Y_OFFSET = 15;
const ZOOM_MIN = 0.85;
const ZOOM_MAX = 2;
const ZOOM_STEP = 0.15;
const ZOOM_DEFAULT = 1.35;

function clampZoom(value: number): number {
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, value));
}

interface LayoutNode {
  node: TaxonomyLineageNode;
  x: number;
  y: number;
  r: number;
  major: boolean;
  selected: boolean;
  /** When true, draws as a sibling stub (lighter, no rank badge, dashed connector). */
  sibling: boolean;
  /** Index into the layout array for connector drawing; null for the root. */
  parentIndex: number | null;
  color: string;
}

function layoutNodes(
  nodes: TaxonomyLineageNode[],
  selectedTaxid: number | undefined,
  expanded: boolean,
  siblings: Record<string, TaxonomyLineageNode[]> | undefined,
): LayoutNode[] {
  const display = expanded
    ? nodes
    : nodes.filter((n) => MAJOR_RANKS.has(n.rank.toLowerCase()));

  const result: LayoutNode[] = [];
  let y = TOP_PAD;

  for (let i = 0; i < display.length; i++) {
    const n = display[i];
    const rankLower = n.rank.toLowerCase();
    const major = MAJOR_RANKS.has(rankLower);
    const r = major ? NODE_R : NODE_R_MINOR;
    const rowH = major ? ROW_H : ROW_H_MINOR;

    const x = LEFT_PAD + i * INDENT;
    y += rowH;

    const lineageIndex = result.length;
    result.push({
      node: n,
      x,
      y,
      r,
      major,
      selected: n.taxid === selectedTaxid,
      sibling: false,
      parentIndex: i === 0 ? null : findPrevLineage(result, lineageIndex),
      color: RANK_COLORS[rankLower] ?? "rgba(154, 163, 184, 0.6)",
    });

    // Inject siblings of this node (other taxa sharing the same parent at
    // the same rank). The backend keys the map by parent taxid.
    if (!siblings || i === 0) continue;
    const parentLineageNode = display[i - 1];
    const sibsForThisRank = siblings[String(parentLineageNode.taxid)];
    if (!sibsForThisRank || sibsForThisRank.length === 0) continue;

    // Siblings branch off the *parent* lineage node, not the current
    // lineage node and not another sibling. We snapshot that index now
    // because `result.length` will advance as siblings are pushed.
    const siblingParentIndex = findPrevLineage(result, lineageIndex);

    for (const sib of sibsForThisRank) {
      if (sib.taxid === n.taxid) continue;
      y += ROW_H_SIBLING;
      result.push({
        node: sib,
        x, // sibling sits at the same indent as the lineage child
        y,
        r: NODE_R_SIBLING,
        major: false,
        selected: false,
        sibling: true,
        parentIndex: siblingParentIndex,
        color: RANK_COLORS[rankLower] ?? "rgba(154, 163, 184, 0.6)",
      });
    }
  }

  return result;
}

/**
 * Returns the index of the most recent non-sibling layout node strictly
 * before `before`, or null if none exists.  Used so connectors anchor on
 * lineage nodes only and never on a sibling stub.
 */
function findPrevLineage(result: LayoutNode[], before: number): number | null {
  for (let j = before - 1; j >= 0; j--) {
    if (!result[j].sibling) return j;
  }
  return null;
}

/* ── component ── */

interface LineageTreeProps {
  nodes: TaxonomyLineageNode[];
  selectedTaxid?: number;
  lineageText?: string;
  /** Siblings keyed by parent taxid (from GET /blast/taxonomy/tree). */
  siblings?: Record<string, TaxonomyLineageNode[]>;
  siblingsLoading?: boolean;
  /** Optional external target for each node; used by read-only detail views. */
  nodeHref?: (taxid: number) => string;
  /** Initial zoom level; the full picker keeps 135%, compact read-only views use 100%. */
  defaultZoom?: number;
  /** Minimum SVG viewBox width; larger values make compact containers less zoomed-in. */
  minSvgWidth?: number;
}

export function LineageTree({
  nodes,
  selectedTaxid,
  lineageText,
  siblings,
  siblingsLoading,
  nodeHref,
  defaultZoom = ZOOM_DEFAULT,
  minSvgWidth = 360,
}: LineageTreeProps) {
  const [expanded, setExpanded] = useState(false);
  const [showSiblings, setShowSiblings] = useState(true);
  const [zoom, setZoom] = useState(() => clampZoom(defaultZoom));

  const majorCount = useMemo(
    () => nodes.filter((n) => MAJOR_RANKS.has(n.rank.toLowerCase())).length,
    [nodes],
  );
  const minorCount = nodes.length - majorCount;
  const hasMinor = minorCount > 0;
  const hasSiblings = !!siblings && Object.keys(siblings).length > 0;

  const laid = useMemo(
    () =>
      layoutNodes(nodes, selectedTaxid, expanded, showSiblings ? siblings : undefined),
    [nodes, selectedTaxid, expanded, showSiblings, siblings],
  );

  if (nodes.length === 0) {
    return (
      <div className="lineage-tree lineage-tree--text">{lineageText || "\u2014"}</div>
    );
  }

  // When collapsed and every rank is minor, `laid` may be empty. Fall back
  // to the text view rather than reading `laid[laid.length - 1]`.
  if (laid.length === 0) {
    return (
      <div className="lineage-tree lineage-tree--text">{lineageText || "\u2014"}</div>
    );
  }

  const lastNode = laid[laid.length - 1];
  const svgHeight = lastNode.y + RANK_Y_OFFSET + 10;
  // Reserve enough horizontal room for the deepest label (longest name ~16 chars).
  const maxX = laid.reduce((m, l) => Math.max(m, l.x), 0);
  const svgWidth = Math.max(minSvgWidth, maxX + 180);
  const zoomPercent = Math.round(zoom * 100);
  const zoomStyle = {
    width: `${zoomPercent}%`,
    minWidth: `${zoomPercent}%`,
  };

  return (
    <div
      className="lineage-tree lineage-tree--cladogram"
      role="tree"
      aria-label="Taxonomic lineage"
    >
      <div className="lineage-tree__header">
        <div className="lineage-tree__toggles">
          {hasMinor && (
            <button
              type="button"
              className="lineage-tree__toggle"
              onClick={() => setExpanded((v) => !v)}
              aria-expanded={expanded}
              title={expanded ? "Show major ranks only" : "Show all ranks"}
            >
              {expanded ? (
                <ChevronDown size={12} strokeWidth={2} />
              ) : (
                <ChevronRight size={12} strokeWidth={2} />
              )}
              <span>
                {expanded ? `All ${nodes.length} ranks` : `${majorCount} major ranks`}
              </span>
              {!expanded && (
                <span className="lineage-tree__toggle-hint">+{minorCount} more</span>
              )}
            </button>
          )}
          {(hasSiblings || siblingsLoading) && (
            <button
              type="button"
              className="lineage-tree__toggle"
              onClick={() => setShowSiblings((v) => !v)}
              aria-pressed={showSiblings}
              disabled={siblingsLoading && !hasSiblings}
              title={
                showSiblings ? "Hide sibling taxa" : "Show sibling taxa at each rank"
              }
            >
              {siblingsLoading && !hasSiblings ? (
                <Loader2 size={12} className="spin" />
              ) : showSiblings ? (
                <ChevronDown size={12} strokeWidth={2} />
              ) : (
                <ChevronRight size={12} strokeWidth={2} />
              )}
              <span>Siblings</span>
              {siblingsLoading && (
                <span className="lineage-tree__toggle-hint">loading…</span>
              )}
            </button>
          )}
        </div>

        <div className="lineage-tree__zoom" aria-label="Lineage zoom controls">
          <button
            type="button"
            className="lineage-tree__zoom-button"
            onClick={() => setZoom((value) => clampZoom(value - ZOOM_STEP))}
            disabled={zoom <= ZOOM_MIN}
            aria-label="Zoom out lineage tree"
            title="Zoom out"
          >
            <ZoomOut size={12} strokeWidth={1.8} />
          </button>
          <span className="lineage-tree__zoom-value">{zoomPercent}%</span>
          <button
            type="button"
            className="lineage-tree__zoom-button"
            onClick={() => setZoom((value) => clampZoom(value + ZOOM_STEP))}
            disabled={zoom >= ZOOM_MAX}
            aria-label="Zoom in lineage tree"
            title="Zoom in"
          >
            <ZoomIn size={12} strokeWidth={1.8} />
          </button>
          <button
            type="button"
            className="lineage-tree__zoom-button"
            onClick={() => setZoom(clampZoom(defaultZoom))}
            disabled={zoom === clampZoom(defaultZoom)}
            aria-label="Reset lineage tree zoom"
            title="Reset zoom"
          >
            <RotateCcw size={12} strokeWidth={1.8} />
          </button>
        </div>
      </div>

      <div
        className="lineage-tree__canvas"
        tabIndex={0}
        aria-label="Scrollable lineage tree canvas"
      >
        <svg
          className="lineage-tree__svg"
          viewBox={`0 0 ${svgWidth} ${svgHeight}`}
          style={zoomStyle}
          preserveAspectRatio="xMinYMin meet"
          role="presentation"
        >
          {/* Connector lines — angled cladogram style */}
          {laid.map((item, i) => {
            if (item.parentIndex === null) return null;
            const parent = laid[item.parentIndex];
            return (
              <path
                key={`c-${i}-${item.node.taxid}`}
                d={`M ${parent.x} ${parent.y} L ${parent.x} ${item.y} L ${item.x} ${item.y}`}
                fill="none"
                stroke={item.color}
                strokeWidth={item.major ? 1.5 : 1}
                strokeOpacity={item.sibling ? 0.16 : item.major ? 0.35 : 0.18}
                strokeDasharray={item.sibling ? "3 3" : undefined}
              />
            );
          })}

          {/* Nodes and labels */}
          {laid.map((item, i) => {
            const content = (
              <g
                className={nodeHref ? "lineage-tree__node-link" : undefined}
                role="treeitem"
              >
                {nodeHref && (
                  <title>{`Open ${item.node.scientific_name} on NCBI Taxonomy`}</title>
                )}
              <circle
                cx={item.x}
                cy={item.y}
                r={item.r}
                fill={item.selected ? item.color : "transparent"}
                stroke={item.color}
                strokeWidth={item.major ? 2 : 1.5}
                opacity={item.sibling ? 0.55 : item.major ? 1 : 0.7}
              />

              {item.selected && (
                <circle
                  cx={item.x}
                  cy={item.y}
                  r={item.r + 3}
                  fill="none"
                  stroke={item.color}
                  strokeWidth={1}
                  opacity={0.3}
                />
              )}

              <text
                x={item.x + item.r + LABEL_GAP}
                y={item.y}
                dominantBaseline="central"
                className={
                  item.selected
                    ? "lineage-tree__svg-label lineage-tree__svg-label--selected"
                    : item.sibling
                      ? "lineage-tree__svg-label lineage-tree__svg-label--minor lineage-tree__svg-label--sibling"
                      : item.major
                        ? "lineage-tree__svg-label"
                        : "lineage-tree__svg-label lineage-tree__svg-label--minor"
                }
                opacity={item.sibling ? 0.65 : 1}
              >
                {item.node.scientific_name}
              </text>

              {item.major && !item.sibling && (
                <text
                  x={item.x + item.r + LABEL_GAP}
                  y={item.y + RANK_Y_OFFSET}
                  className="lineage-tree__svg-rank"
                  fill={item.color}
                >
                  {item.node.rank}
                </text>
              )}
              </g>
            );

            return nodeHref ? (
              <a
                key={`n-${i}-${item.node.taxid}`}
                href={nodeHref(item.node.taxid)}
                target="_blank"
                rel="noopener noreferrer"
                aria-label={`Open ${item.node.scientific_name} on NCBI Taxonomy`}
              >
                {content}
              </a>
            ) : (
              <g key={`n-${i}-${item.node.taxid}`}>{content}</g>
            );
          })}
        </svg>
      </div>
    </div>
  );
}
