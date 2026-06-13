/**
 * Sequence Detail page — leaf presentational parts.
 *
 * Small building blocks rendered by the `SequenceDetail` page: metadata
 * cells, the record-trust pill, the truncation marker, a copy button, and
 * the expandable feature row + qualifier pair. Pure presentation; record
 * derivation lives in `sequenceRecord.ts` and the page owns the data.
 */

import { useState, type CSSProperties } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, Check, ChevronDown, ChevronRight, Copy } from "lucide-react";

import type { NuccoreFeature } from "@/api/ncbi";
import { useTransientState } from "../../hooks/useTransientState";
import {
  dbXrefUrl,
  featureLabel,
  featureRange,
  featureSummary,
  type TrustBadge,
} from "./sequenceRecord";

export function NewTabHint() {
  return <span className="sr-only"> (opens in new tab)</span>;
}

export function MetaCell({
  label,
  value,
  hideEmpty,
}: {
  label: string;
  value: string | null | undefined;
  hideEmpty?: boolean;
}) {
  const empty = value == null || value === "";
  if (empty && hideEmpty) return null;
  return (
    <div style={{ display: "grid", gap: 2 }}>
      <dt style={{ fontSize: 11, color: "var(--text-secondary, var(--text-muted))" }}>
        {label}
      </dt>
      <dd
        style={{
          margin: 0,
          fontFamily: "var(--font-mono, monospace)",
        }}
      >
        {empty ? "—" : value}
      </dd>
    </div>
  );
}

// Record-trust pill. ``warn`` badges carry a muted warning tint; ``ok`` badges
// stay in the calm grey/teal family per the glass design rules. When a badge
// points at a replacing accession it renders as an internal Link so the user
// can jump straight to the current record.
export function TrustBadgePill({ badge }: { badge: TrustBadge }) {
  const tint =
    badge.tone === "warn"
      ? { color: "var(--warning)", border: "var(--warning)" }
      : { color: "var(--text-muted)", border: "var(--border, var(--text-muted))" };
  const style: CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: 5,
    fontSize: 11,
    lineHeight: 1.2,
    padding: "3px 9px",
    borderRadius: 999,
    border: `1px solid color-mix(in srgb, ${tint.border} 45%, transparent)`,
    background: `color-mix(in srgb, ${tint.color} 10%, transparent)`,
    color: tint.color,
    textDecoration: "none",
    whiteSpace: "nowrap",
  };
  const inner = (
    <>
      {badge.tone === "warn" ? (
        <AlertTriangle size={11} strokeWidth={1.5} />
      ) : (
        <Check size={11} strokeWidth={1.5} />
      )}
      <span style={{ fontWeight: 500 }}>{badge.label}</span>
    </>
  );
  if (badge.to) {
    return (
      <Link to={badge.to} title={badge.title} style={style}>
        {inner}
      </Link>
    );
  }
  return (
    <span title={badge.title} style={style}>
      {inner}
    </span>
  );
}

// Inline "this value was clipped" marker. Points the researcher at the full
// record on NCBI so a truncated value is never mistaken for the whole.
export function TruncationNote({ href }: { href: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title="This value was clipped for display. Open the full record on NCBI."
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        fontSize: 11,
        color: "var(--warning)",
        textDecoration: "none",
        whiteSpace: "nowrap",
      }}
    >
      <AlertTriangle size={11} strokeWidth={1.5} />
      truncated — view full on NCBI
    </a>
  );
}

// Copy-to-clipboard control. Mirrors NCBI's per-field copy affordance so a
// researcher can grab the accession or the FASTA without manual selection.
// Falls back silently if the Clipboard API is unavailable (older browsers /
// insecure context) — the button simply does nothing rather than throwing.
export function CopyButton({
  value,
  label,
  title,
}: {
  value: string;
  label: string;
  title?: string;
}) {
  const [copied, flashCopied] = useTransientState(false);
  const onCopy = () => {
    if (!navigator.clipboard?.writeText) return;
    void navigator.clipboard.writeText(value).then(() => {
      flashCopied(true, 1500);
    });
  };
  return (
    <button
      type="button"
      className="glass-button glass-button--ghost"
      onClick={onCopy}
      title={title || `Copy ${label}`}
      style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, padding: "2px 8px" }}
    >
      {copied ? <Check size={12} strokeWidth={1.5} /> : <Copy size={12} strokeWidth={1.5} />}
      {copied ? "Copied" : label}
    </button>
  );
}

// A single feature row plus an expandable panel that lists every qualifier.
// NCBI's nuccore page shows the full qualifier set (mol_type, isolate,
// db_xref, translation, …); the collapsed dashboard table only surfaces
// gene/product/note, so the toggle reveals parity on demand.
export function FeatureRow({
  feature,
  nuccoreUrl,
  onBlastRange,
}: {
  feature: NuccoreFeature;
  nuccoreUrl: string;
  onBlastRange: (range: { start: number; stop: number }) => void;
}) {
  const [open, setOpen] = useState(false);
  const range = featureRange(feature);
  const hasQualifiers = feature.qualifiers.length > 0;
  return (
    <>
      <tr>
        <td style={{ textAlign: "center" }}>
          {hasQualifiers && (
            <button
              type="button"
              aria-label={open ? "Collapse qualifiers" : "Expand qualifiers"}
              aria-expanded={open}
              onClick={() => setOpen((prev) => !prev)}
              style={{
                background: "none",
                border: "none",
                cursor: "pointer",
                color: "var(--text-muted)",
                padding: 0,
                display: "inline-flex",
              }}
            >
              {open ? (
                <ChevronDown size={14} strokeWidth={1.5} />
              ) : (
                <ChevronRight size={14} strokeWidth={1.5} />
              )}
            </button>
          )}
        </td>
        <td style={{ fontFamily: "var(--font-mono, monospace)" }}>{featureLabel(feature)}</td>
        <td style={{ fontFamily: "var(--font-mono, monospace)" }}>{feature.location || "—"}</td>
        <td>{featureSummary(feature) || "—"}</td>
        <td style={{ textAlign: "right" }}>
          {range && (
            <button
              type="button"
              className="glass-button glass-button--ghost"
              style={{ fontSize: 11, padding: "2px 8px" }}
              aria-label={`BLAST the ${featureLabel(feature)} feature range ${range.start} to ${range.stop}`}
              onClick={() => onBlastRange(range)}
            >
              BLAST range
            </button>
          )}
        </td>
      </tr>
      {open && hasQualifiers && (
        <tr>
          <td />
          <td colSpan={4} style={{ paddingBottom: 10 }}>
            <dl
              style={{
                margin: 0,
                display: "grid",
                gridTemplateColumns: "minmax(120px, max-content) 1fr",
                gap: "2px 12px",
                fontSize: 11,
              }}
            >
              {feature.qualifiers.map((qual, qIdx) => (
                <FragmentQualifier
                  key={`${qual.name}-${qIdx}`}
                  name={qual.name}
                  value={qual.value}
                  truncated={qual.truncated}
                  nuccoreUrl={nuccoreUrl}
                />
              ))}
            </dl>
          </td>
        </tr>
      )}
    </>
  );
}

// One qualifier key/value pair. ``db_xref`` values are linked to the matching
// NCBI database (Taxonomy / Gene) when recognised. When the backend clipped the
// value (e.g. a long ``translation``), a marker links to the full NCBI record.
function FragmentQualifier({
  name,
  value,
  truncated,
  nuccoreUrl,
}: {
  name: string;
  value: string | null;
  truncated?: boolean;
  nuccoreUrl: string;
}) {
  const isDbXref = name === "db_xref" && value != null;
  const linked = isDbXref ? dbXrefUrl(value as string) : null;
  return (
    <>
      <dt style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono, monospace)" }}>
        /{name}
      </dt>
      <dd style={{ margin: 0, wordBreak: "break-word" }}>
        {linked?.href ? (
          <a href={linked.href} target="_blank" rel="noopener noreferrer">
            {linked.label}
          </a>
        ) : (
          value || "—"
        )}
        {truncated && (
          <>
            {" "}
            <TruncationNote href={nuccoreUrl} />
          </>
        )}
      </dd>
    </>
  );
}
