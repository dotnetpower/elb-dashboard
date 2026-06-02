import { useMemo } from "react";
import { Link, useParams, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink, ArrowLeft, Play, AlertTriangle, Maximize2 } from "lucide-react";

import {
  getNuccoreSummary,
  getNuccoreGenBank,
  getNuccoreFasta,
  type NuccoreFeature,
  type NuccoreGenBank,
} from "@/api/ncbi";

const NCBI_NUCCORE_BASE = "https://www.ncbi.nlm.nih.gov/nuccore";
const NCBI_SVIEWER_BASE = "https://www.ncbi.nlm.nih.gov/projects/sviewer/sviewer.cgi";

const SEQUENCE_PREVIEW_BYTES = 8_000;
// Whole-sequence accession BLAST is rejected by the submit pipeline when the
// resolved FASTA exceeds 5 MiB (see ``ncbi_query_too_large`` in
// ``api/services/blast/accession_resolver.py``). We surface a confirm dialog
// before navigating to BLAST submit so researchers can opt out instead of
// hitting the error after the round trip.
const MAX_WHOLE_SEQUENCE_NT = 5_000_000;

function externalNuccoreUrl(accession: string): string {
  return `${NCBI_NUCCORE_BASE}/${encodeURIComponent(accession)}`;
}

function sviewerEmbedUrl(
  accession: string,
  highlight?: { start: number; stop: number } | null,
): string {
  const params = new URLSearchParams();
  params.set("id", accession);
  params.set("tracks", "[key:sequence_track,name:Sequence][key:gene_model_track]");
  if (highlight) {
    params.set("v", `${highlight.start}:${highlight.stop}`);
    params.set("mk", `${highlight.start}:${highlight.stop}|hit`);
  }
  return `${NCBI_SVIEWER_BASE}?${params.toString()}`;
}

function formatInteger(value: number | null | undefined): string {
  if (value == null) return "—";
  return value.toLocaleString();
}

function featureLabel(feature: NuccoreFeature): string {
  return feature.key || "feature";
}

/** First value of a named qualifier on a feature, or null. */
function qualifier(feature: NuccoreFeature, name: string): string | null {
  for (const qual of feature.qualifiers) {
    if (qual.name === name && qual.value) return qual.value;
  }
  return null;
}

function featureSummary(feature: NuccoreFeature): string {
  const parts: string[] = [];
  const gene = qualifier(feature, "gene");
  const product = qualifier(feature, "product");
  const note = qualifier(feature, "note");
  if (gene) parts.push(gene);
  if (product) parts.push(product);
  if (note && parts.length === 0) parts.push(note);
  return parts.join(" — ");
}

function featureRange(feature: NuccoreFeature): { start: number; stop: number } | null {
  for (const interval of feature.intervals) {
    if (interval.start != null && interval.stop != null) {
      return { start: interval.start, stop: interval.stop };
    }
  }
  return null;
}

// NCBI renders sample provenance from the ``source`` feature qualifiers. We
// surface the same fields the public nuccore record shows so the dashboard
// detail page reads like the NCBI page. ``country`` is the legacy name for
// what NCBI now labels ``geo_loc_name``; accept either.
const SOURCE_QUALIFIER_FIELDS: ReadonlyArray<{ label: string; names: string[] }> = [
  { label: "Mol type", names: ["mol_type"] },
  { label: "Isolate", names: ["isolate"] },
  { label: "Strain", names: ["strain"] },
  { label: "Host", names: ["host"] },
  { label: "Geo location", names: ["geo_loc_name", "country"] },
  { label: "Collection date", names: ["collection_date"] },
  { label: "Collected by", names: ["collected_by"] },
  { label: "Isolation source", names: ["isolation_source"] },
];

function sourceFeature(genbank: NuccoreGenBank | undefined): NuccoreFeature | null {
  if (!genbank) return null;
  return genbank.features.find((feature) => feature.key === "source") ?? null;
}

function firstQualifier(
  feature: NuccoreFeature | null,
  names: string[],
): string | null {
  if (!feature) return null;
  for (const name of names) {
    const value = qualifier(feature, name);
    if (value) return value;
  }
  return null;
}

const NCBI_BIOPROJECT_BASE = "https://www.ncbi.nlm.nih.gov/bioproject";
const NCBI_BIOSAMPLE_BASE = "https://www.ncbi.nlm.nih.gov/biosample";
const NCBI_PUBMED_BASE = "https://pubmed.ncbi.nlm.nih.gov";

function xrefUrl(dbname: string, id: string): string | null {
  const key = dbname.toLowerCase();
  if (key === "bioproject") return `${NCBI_BIOPROJECT_BASE}/${encodeURIComponent(id)}`;
  if (key === "biosample") return `${NCBI_BIOSAMPLE_BASE}/${encodeURIComponent(id)}`;
  return null;
}

export function SequenceDetail() {
  const params = useParams<{ accession: string }>();
  const accession = (params.accession || "").trim();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const highlightStart = Number.parseInt(searchParams.get("hl_start") || "", 10);
  const highlightStop = Number.parseInt(searchParams.get("hl_stop") || "", 10);
  const hasHighlight =
    Number.isFinite(highlightStart) &&
    Number.isFinite(highlightStop) &&
    highlightStart > 0 &&
    highlightStop >= highlightStart;

  const summaryQuery = useQuery({
    queryKey: ["ncbi", "summary", accession],
    queryFn: () => getNuccoreSummary(accession),
    enabled: accession.length > 0,
    staleTime: 5 * 60_000,
  });

  // The summary call is cheap (single ESummary round trip). GenBank parsing
  // and FASTA download are heavier and only meaningful once the summary
  // confirmed the accession exists, so we chain ``enabled`` to avoid firing
  // three parallel NCBI calls for a typo (e.g. `XX_999999.9`) — under the
  // shared 3 req/s rate limit that would burn the entire window.
  const genbankQuery = useQuery({
    queryKey: ["ncbi", "genbank", accession],
    queryFn: () => getNuccoreGenBank(accession),
    enabled: accession.length > 0 && !!summaryQuery.data,
    staleTime: 5 * 60_000,
  });

  const fastaQuery = useQuery({
    queryKey: ["ncbi", "fasta", accession],
    queryFn: () => getNuccoreFasta(accession),
    enabled: accession.length > 0 && !!genbankQuery.data,
    staleTime: 5 * 60_000,
  });

  const summary = summaryQuery.data;
  const genbank = genbankQuery.data;
  const fasta = fastaQuery.data;
  const highlightRange = hasHighlight
    ? { start: highlightStart, stop: highlightStop }
    : null;

  const previewFasta = useMemo(() => {
    if (!fasta) return null;
    if (fasta.length <= SEQUENCE_PREVIEW_BYTES) return fasta;
    return `${fasta.slice(0, SEQUENCE_PREVIEW_BYTES)}\n…(truncated, ${(fasta.length - SEQUENCE_PREVIEW_BYTES).toLocaleString()} bytes hidden)`;
  }, [fasta]);

  const source = sourceFeature(genbank);
  const sourceRows = useMemo(
    () =>
      SOURCE_QUALIFIER_FIELDS.map((field) => ({
        label: field.label,
        value: firstQualifier(source, field.names),
      })).filter((row) => row.value != null),
    [source],
  );
  const lineage = useMemo(() => {
    const raw = genbank?.taxonomy_lineage || "";
    return raw
      .split(";")
      .map((part) => part.trim())
      .filter((part) => part.length > 0);
  }, [genbank?.taxonomy_lineage]);

  if (!accession) {
    return (
      <div className="glass-card glass-card--strong" style={{ padding: 24 }}>
        <h2 style={{ marginTop: 0 }}>Missing accession</h2>
        <p className="muted">No NCBI accession was provided in the URL.</p>
        <Link className="glass-button" to="/">
          <ArrowLeft size={14} /> Back to dashboard
        </Link>
      </div>
    );
  }

  const launchBlast = () => {
    // Length guard: warn before navigating when the whole-sequence FASTA
    // would exceed the submit pipeline's 5 MiB cap. Sub-range BLAST always
    // proceeds because the caller already picked a window.
    if (!hasHighlight && summary === undefined) {
      // Summary still loading: the button is rendered disabled in this
      // state, but defend the click path against any future re-entry.
      return;
    }
    if (!hasHighlight && summary?.length && summary.length > MAX_WHOLE_SEQUENCE_NT) {
      const confirmed = window.confirm(
        `Whole-sequence BLAST for ${accession} (${summary.length.toLocaleString()} nt)` +
          ` exceeds the 5,000,000 nt limit and will fail at submit with` +
          ` "ncbi_query_too_large". Pick a sub-range from the hits table or` +
          ` BLAST a slice manually instead. Continue anyway?`,
      );
      if (!confirmed) return;
    }
    const params = new URLSearchParams();
    params.set("accession", accession);
    if (hasHighlight) {
      params.set("from", String(highlightStart));
      params.set("to", String(highlightStop));
    }
    navigate(`/blast/submit?${params.toString()}`);
  };

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div
        className="glass-card glass-card--strong"
        style={{ padding: 20, display: "grid", gap: 12 }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "flex-start",
            justifyContent: "space-between",
            gap: 16,
            flexWrap: "wrap",
          }}
        >
          <div style={{ minWidth: 0 }}>
            <div
              style={{
                display: "flex",
                gap: 6,
                alignItems: "center",
                fontSize: 12,
                color: "var(--text-muted)",
              }}
            >
              <Link to="/blast/jobs" style={{ color: "inherit" }}>
                ← BLAST jobs
              </Link>
              <span>/</span>
              <span>Sequence</span>
            </div>
            <h1
              style={{
                margin: "4px 0 0",
                fontSize: 22,
                fontFamily: "var(--font-mono, monospace)",
                wordBreak: "break-all",
              }}
            >
              {summary?.accession_version || accession}
            </h1>
            <div
              style={{
                marginTop: 6,
                fontSize: 13,
                color: "var(--text)",
                lineHeight: 1.4,
              }}
            >
              {summary?.title || genbank?.definition || (
                <span className="muted">Loading metadata…</span>
              )}
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button
              type="button"
              className="glass-button glass-button--primary"
              onClick={launchBlast}
              disabled={!hasHighlight && summary === undefined}
              title={
                !hasHighlight && summary === undefined
                  ? "Waiting for NCBI summary before BLAST handoff"
                  : undefined
              }
            >
              <Play size={14} strokeWidth={1.5} /> Use in BLAST
            </button>
            <a
              className="glass-button"
              href={externalNuccoreUrl(accession)}
              target="_blank"
              rel="noopener noreferrer"
            >
              <ExternalLink size={14} strokeWidth={1.5} /> Open in NCBI
            </a>
          </div>
        </div>

        {(summaryQuery.isError || genbankQuery.isError || fastaQuery.isError) && (
          <div
            className="glass-card"
            role="alert"
            style={{
              padding: "10px 12px",
              display: "flex",
              alignItems: "center",
              gap: 8,
              color: "var(--warning)",
              fontSize: 12,
            }}
          >
            <AlertTriangle size={13} strokeWidth={1.5} />
            <span>
              One or more NCBI lookups failed. The dashboard does not cache
              upstream outages — retry in a moment.
            </span>
          </div>
        )}

        <dl
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
            gap: 12,
            margin: 0,
            fontSize: 13,
          }}
        >
          <MetaCell label="Organism" value={summary?.organism || genbank?.organism} />
          <MetaCell label="Taxid" value={summary?.taxid != null ? String(summary.taxid) : null} />
          <MetaCell label="Length (bp/aa)" value={formatInteger(summary?.length ?? genbank?.length ?? null)} />
          <MetaCell label="Molecule" value={summary?.moltype || genbank?.moltype} />
          <MetaCell label="Topology" value={summary?.topology || genbank?.topology} />
          <MetaCell label="Updated" value={summary?.update_date || genbank?.update_date} />
        </dl>
      </div>

      {(sourceRows.length > 0 || (genbank?.xrefs.length ?? 0) > 0) && (
        <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 12 }}>
          <h2 style={{ margin: 0, fontSize: 14 }}>Sample &amp; source</h2>
          {sourceRows.length > 0 && (
            <dl
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
                gap: 12,
                margin: 0,
                fontSize: 13,
              }}
            >
              {sourceRows.map((row) => (
                <MetaCell key={row.label} label={row.label} value={row.value} />
              ))}
            </dl>
          )}
          {genbank && genbank.xrefs.length > 0 && (
            <div style={{ display: "grid", gap: 6 }}>
              <span style={{ fontSize: 11, color: "var(--text-muted)" }}>DBLINK</span>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {genbank.xrefs.map((xref) => {
                  const href = xrefUrl(xref.dbname, xref.id);
                  const label = `${xref.dbname}: ${xref.id}`;
                  return href ? (
                    <a
                      key={`${xref.dbname}-${xref.id}`}
                      className="glass-button glass-button--ghost"
                      href={href}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{
                        fontSize: 12,
                        padding: "2px 8px",
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 4,
                      }}
                    >
                      <ExternalLink size={11} strokeWidth={1.5} />
                      {label}
                    </a>
                  ) : (
                    <span
                      key={`${xref.dbname}-${xref.id}`}
                      style={{
                        fontSize: 12,
                        padding: "2px 8px",
                        fontFamily: "var(--font-mono, monospace)",
                        color: "var(--text-muted)",
                      }}
                    >
                      {label}
                    </span>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}

      {lineage.length > 0 && (
        <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 8 }}>
          <h2 style={{ margin: 0, fontSize: 14 }}>Taxonomy</h2>
          <div style={{ fontSize: 12, lineHeight: 1.7, color: "var(--text)" }}>
            {lineage.map((rank, idx) => (
              <span key={`${rank}-${idx}`}>
                {idx > 0 && <span style={{ color: "var(--text-muted)" }}> › </span>}
                {rank}
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 10 }}>
        <h2 style={{ margin: 0, fontSize: 14 }}>Features</h2>
        {genbankQuery.isLoading && <p className="muted" style={{ margin: 0 }}>Loading features…</p>}
        {genbank && genbank.features.length === 0 && (
          <p className="muted" style={{ margin: 0 }}>No features reported.</p>
        )}
        {genbank && genbank.features.length > 0 && (
          <div style={{ overflowX: "auto" }}>
            <table className="glass-table" style={{ width: "100%", fontSize: 12 }}>
              <thead>
                <tr>
                  <th style={{ textAlign: "left" }}>Key</th>
                  <th style={{ textAlign: "left" }}>Location</th>
                  <th style={{ textAlign: "left" }}>Gene / Product</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {genbank.features.slice(0, 200).map((feature, idx) => {
                  const range = featureRange(feature);
                  return (
                    <tr key={`${feature.key}-${idx}`}>
                      <td style={{ fontFamily: "var(--font-mono, monospace)" }}>
                        {featureLabel(feature)}
                      </td>
                      <td style={{ fontFamily: "var(--font-mono, monospace)" }}>
                        {feature.location || "—"}
                      </td>
                      <td>{featureSummary(feature) || "—"}</td>
                      <td style={{ textAlign: "right" }}>
                        {range && (
                          <button
                            type="button"
                            className="glass-button glass-button--ghost"
                            style={{ fontSize: 11, padding: "2px 8px" }}
                            onClick={() => {
                              const search = new URLSearchParams({
                                accession,
                                from: String(range.start),
                                to: String(range.stop),
                              });
                              navigate(`/blast/submit?${search.toString()}`);
                            }}
                          >
                            BLAST range
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {genbank.features.length > 200 && (
              <p className="muted" style={{ margin: "6px 0 0", fontSize: 11 }}>
                Showing first 200 of {genbank.features.length} features. Open in NCBI to see the rest.
              </p>
            )}
          </div>
        )}
      </div>

      {genbank && genbank.references.length > 0 && (
        <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 10 }}>
          <h2 style={{ margin: 0, fontSize: 14 }}>References</h2>
          <ol style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 10 }}>
            {genbank.references.slice(0, 24).map((ref, idx) => (
              <li key={`ref-${idx}`} style={{ fontSize: 12, lineHeight: 1.5 }}>
                {ref.title && <div style={{ color: "var(--text)" }}>{ref.title}</div>}
                {ref.authors.length > 0 && (
                  <div className="muted">{ref.authors.join(", ")}</div>
                )}
                {ref.journal && <div className="muted">{ref.journal}</div>}
                {ref.pubmed && (
                  <a
                    href={`${NCBI_PUBMED_BASE}/${encodeURIComponent(ref.pubmed)}/`}
                    target="_blank"
                    rel="noopener noreferrer"
                    style={{ display: "inline-flex", alignItems: "center", gap: 4, marginTop: 2 }}
                  >
                    <ExternalLink size={11} strokeWidth={1.5} />
                    PubMed {ref.pubmed}
                  </a>
                )}
              </li>
            ))}
          </ol>
        </div>
      )}

      <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 8 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
          <h2 style={{ margin: 0, fontSize: 14 }}>Sequence (FASTA preview)</h2>
          {hasHighlight && (
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
              Hit range {highlightStart.toLocaleString()}–{highlightStop.toLocaleString()} requested
            </span>
          )}
        </div>
        {fastaQuery.isLoading && <p className="muted" style={{ margin: 0 }}>Loading FASTA…</p>}
        {previewFasta && (
          <pre
            style={{
              margin: 0,
              maxHeight: 320,
              overflow: "auto",
              padding: "10px 12px",
              borderRadius: 8,
              background: "rgba(0,0,0,0.18)",
              fontSize: 12,
              fontFamily: "var(--font-mono, monospace)",
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
            }}
          >
            {previewFasta}
          </pre>
        )}
      </div>

      <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 8 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <h2 style={{ margin: 0, fontSize: 14 }}>
            <Maximize2 size={13} strokeWidth={1.5} style={{ verticalAlign: "-2px", marginRight: 4 }} />
            Advanced view (NCBI Sequence Viewer)
          </h2>
          {/* NCBI serves every page with ``X-Frame-Options: SAMEORIGIN``, so
              the Sequence Viewer cannot be embedded cross-origin in an
              iframe — the browser refuses to render it ("connection
              refused"). Open it in a new tab on the NCBI origin instead. The
              dashboard sends no data; the link only carries the accession
              {hasHighlight ? " and hit range" : ""}. */}
          <a
            className="glass-button"
            href={sviewerEmbedUrl(accession, highlightRange)}
            target="_blank"
            rel="noopener noreferrer"
            style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
          >
            <ExternalLink size={13} strokeWidth={1.5} />
            Open Sequence Viewer
          </a>
        </div>
        <p className="muted" style={{ margin: 0, fontSize: 12 }}>
          NCBI blocks embedding the Sequence Viewer in other sites, so it opens
          in a new tab for full pan/zoom and track inspection. The dashboard
          does not send any data; the link only carries the accession
          {hasHighlight ? " and hit range" : ""}.
        </p>
      </div>
    </div>
  );
}

function MetaCell({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div style={{ display: "grid", gap: 2 }}>
      <dt style={{ fontSize: 11, color: "var(--text-muted)" }}>{label}</dt>
      <dd
        style={{
          margin: 0,
          fontFamily: label === "Updated" ? "inherit" : "var(--font-mono, monospace)",
        }}
      >
        {value || "—"}
      </dd>
    </div>
  );
}

export default SequenceDetail;
