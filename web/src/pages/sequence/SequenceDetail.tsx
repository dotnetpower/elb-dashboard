import { useMemo, useState } from "react";
import { Link, useParams, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink, ArrowLeft, Play, AlertTriangle, Maximize2 } from "lucide-react";

import {
  getNuccoreSummary,
  getNuccoreGenBank,
  getNuccoreFasta,
} from "@/api/ncbi";
import { SViewerEmbed } from "./SViewerEmbed";
import { SequenceBlocks } from "./SequenceBlocks";
import { GenBankFlatBlock } from "./GenBankFlatBlock";
import { TargetAnalysisCard } from "./TargetAnalysisCard";
import { JobBackReferenceCard } from "./JobBackReferenceCard";
import {
  buildRelatedResources,
  collectGeneInfo,
  deriveTrustBadges,
  DOI_BASE,
  externalNuccoreUrl,
  FEATURE_DISPLAY_LIMIT,
  firstQualifier,
  formatInteger,
  genbankFlatLines,
  MAX_WHOLE_SEQUENCE_NT,
  NCBI_PUBMED_BASE,
  SOURCE_QUALIFIER_FIELDS,
  sourceFeature,
  sviewerEmbedUrl,
  xrefUrl,
} from "./sequenceRecord";
import {
  CopyButton,
  FeatureRow,
  MetaCell,
  NewTabHint,
  TrustBadgePill,
  TruncationNote,
} from "./sequenceDetailParts";

export function SequenceDetail() {
  const params = useParams<{ accession: string }>();
  const accession = (params.accession || "").trim();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [hideGaps, setHideGaps] = useState(true);
  const [showAllFeatures, setShowAllFeatures] = useState(false);

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

  // Show the full resolved FASTA; the record is already bounded by the
  // backend fetch byte caps (MAX_FASTA_BYTES), so no display truncation here.
  const previewFasta = fasta;

  // Whitespace-free residue string (deflines stripped) for the target-analysis
  // card's composition / sub-range math. Null until the FASTA resolves.
  const sequenceResidues = useMemo(() => {
    if (!fasta) return null;
    return fasta
      .split(/\r?\n/)
      .filter((line) => line.length > 0 && !line.startsWith(">"))
      .join("")
      .replace(/\s+/g, "");
  }, [fasta]);

  // Features table: assembly_gap rows dominate draft genome records and bury
  // the annotated gene/CDS features. Default to hiding them with a count chip
  // to restore them, and cap the initial render so a feature-rich record does
  // not produce an endless table.
  const allFeatures = genbank?.features ?? [];
  const gapFeatureCount = allFeatures.filter(
    (f) => f.key === "assembly_gap" || f.key === "gap",
  ).length;
  const visibleFeatures = hideGaps
    ? allFeatures.filter((f) => f.key !== "assembly_gap" && f.key !== "gap")
    : allFeatures;
  const shownFeatures = showAllFeatures
    ? visibleFeatures
    : visibleFeatures.slice(0, FEATURE_DISPLAY_LIMIT);

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

  const flatRecord = useMemo(
    () => (genbank ? genbankFlatLines(genbank).join("\n") : null),
    [genbank],
  );
  const flatLines = useMemo(
    () => (genbank ? genbankFlatLines(genbank) : null),
    [genbank],
  );

  const jumpToHit = () => {
    const el = document.getElementById("sequence-section");
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const relatedResources = useMemo(() => {
    const { symbols, geneId } = collectGeneInfo(genbank);
    return buildRelatedResources({
      symbols,
      geneId,
      taxid: summary?.taxid ?? null,
      organism: summary?.organism ?? genbank?.organism ?? null,
    });
  }, [genbank, summary?.taxid, summary?.organism]);

  const trustBadges = useMemo(
    () => deriveTrustBadges(summary, genbank),
    [summary, genbank],
  );

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
            {trustBadges.length > 0 && (
              <div
                style={{
                  marginTop: 8,
                  display: "flex",
                  flexWrap: "wrap",
                  gap: 6,
                }}
              >
                {trustBadges.map((badge) => (
                  <TrustBadgePill key={badge.label} badge={badge} />
                ))}
              </div>
            )}
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
              <NewTabHint />
            </a>
            <CopyButton
              value={summary?.accession_version || accession}
              label="Copy accession"
            />
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
              {[
                summaryQuery.isError ? "summary" : null,
                genbankQuery.isError ? "GenBank record" : null,
                fastaQuery.isError ? "FASTA" : null,
              ]
                .filter(Boolean)
                .join(", ")}{" "}
              lookup failed. The dashboard does not cache upstream outages —
              retry in a moment.
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
          <MetaCell label="Strandedness" value={genbank?.strandedness} hideEmpty />
          <MetaCell label="Biomol" value={summary?.biomol} hideEmpty />
          <MetaCell label="Completeness" value={summary?.completeness} hideEmpty />
          <MetaCell label="Division" value={genbank?.division} hideEmpty />
          <MetaCell label="Source DB" value={summary?.source_db} hideEmpty />
          <MetaCell label="Created" value={summary?.create_date || genbank?.create_date} />
          <MetaCell label="Updated" value={summary?.update_date || genbank?.update_date} />
        </dl>
      </div>

      <JobBackReferenceCard accession={accession} />

      <TargetAnalysisCard
        accession={accession}
        seq={sequenceResidues}
        highlight={highlightRange}
        features={genbank?.features ?? []}
        summary={summary}
        genbank={genbank}
        onJumpToHit={hasHighlight ? jumpToHit : undefined}
      />

      {flatRecord && flatLines && (
        <section
          className="glass-card glass-card--strong"
          style={{ padding: 16, display: "grid", gap: 10 }}
          aria-labelledby="genbank-heading"
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 8,
              flexWrap: "wrap",
            }}
          >
            <h2 id="genbank-heading" style={{ margin: 0, fontSize: 14 }}>
              GenBank record
            </h2>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
              Header block in the NCBI flat-file layout
            </span>
          </div>
          {genbank?.truncated_fields?.some((f) =>
            f === "definition" || f === "taxonomy_lineage",
          ) && (
            <TruncationNote href={externalNuccoreUrl(accession)} />
          )}
          <GenBankFlatBlock lines={flatLines} rawText={flatRecord} />
        </section>
      )}

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
        <section
          className="glass-card glass-card--strong"
          style={{ padding: 16, display: "grid", gap: 8 }}
          aria-labelledby="taxonomy-heading"
        >
          <h2 id="taxonomy-heading" style={{ margin: 0, fontSize: 14 }}>
            Taxonomy
          </h2>
          <div style={{ fontSize: 12, lineHeight: 1.9, color: "var(--text)" }}>
            {lineage.map((rank, idx) => (
              <span key={`${rank}-${idx}`}>
                {idx > 0 && (
                  <span aria-hidden="true" style={{ color: "var(--text-muted)" }}>
                    {" › "}
                  </span>
                )}
                <a
                  href={`https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?name=${encodeURIComponent(rank)}`}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {rank}
                  <NewTabHint />
                </a>
              </span>
            ))}
          </div>
        </section>
      )}

      {relatedResources.length > 0 && (
        <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 10 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 8,
              flexWrap: "wrap",
            }}
          >
            <h2 style={{ margin: 0, fontSize: 14 }}>Related NCBI resources</h2>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
              Opens on the NCBI origin in a new tab
            </span>
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {relatedResources.map((resource) => (
              <a
                key={resource.label}
                className="glass-button glass-button--ghost"
                href={resource.href}
                target="_blank"
                rel="noopener noreferrer"
                title={
                  resource.confidence === "search"
                    ? `${resource.hint} — NCBI search (may return several records)`
                    : resource.hint
                }
                style={{
                  fontSize: 12,
                  padding: "4px 10px",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                }}
              >
                <ExternalLink size={12} strokeWidth={1.5} />
                <span style={{ fontWeight: 500 }}>{resource.label}</span>
                <span style={{ color: "var(--text-muted)" }}>· {resource.hint}</span>
                {resource.confidence === "search" && (
                  <span
                    style={{
                      fontSize: 10,
                      color: "var(--text-muted)",
                      border: "1px solid color-mix(in srgb, var(--text-muted) 35%, transparent)",
                      borderRadius: 4,
                      padding: "0 4px",
                      lineHeight: 1.5,
                    }}
                  >
                    search
                  </span>
                )}
              </a>
            ))}
          </div>
        </div>
      )}

      {genbank?.comment && (
        <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 8 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 8,
              flexWrap: "wrap",
            }}
          >
            <h2 style={{ margin: 0, fontSize: 14 }}>Comment</h2>
            {genbank.truncated_fields?.includes("comment") && (
              <TruncationNote href={externalNuccoreUrl(accession)} />
            )}
          </div>
          <p
            style={{
              margin: 0,
              fontSize: 12,
              lineHeight: 1.6,
              color: "var(--text)",
              whiteSpace: "pre-wrap",
            }}
          >
            {genbank.comment}
          </p>
        </div>
      )}

      <section
        className="glass-card glass-card--strong"
        style={{ padding: 16, display: "grid", gap: 10 }}
        aria-labelledby="features-heading"
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 8,
            flexWrap: "wrap",
          }}
        >
          <h2 id="features-heading" style={{ margin: 0, fontSize: 14 }}>
            Features
          </h2>
          {gapFeatureCount > 0 && (
            <button
              type="button"
              className="glass-button glass-button--ghost"
              style={{ fontSize: 11, padding: "2px 8px" }}
              aria-pressed={!hideGaps}
              onClick={() => setHideGaps((v) => !v)}
            >
              {hideGaps
                ? `Show ${gapFeatureCount} assembly gaps`
                : `Hide ${gapFeatureCount} assembly gaps`}
            </button>
          )}
        </div>
        {genbankQuery.isLoading && <p className="muted" style={{ margin: 0 }}>Loading features…</p>}
        {genbank && genbank.features.length === 0 && (
          <p className="muted" style={{ margin: 0 }}>No features reported.</p>
        )}
        {genbank && genbank.features.length > 0 && (
          <div style={{ overflowX: "auto" }}>
            <table className="glass-table" style={{ width: "100%", fontSize: 12 }}>
              <caption className="sr-only">
                GenBank annotated features for {accession}: key, location, and
                gene or product. Each row can be expanded to show its full
                qualifier set.
              </caption>
              <thead>
                <tr>
                  <th scope="col" style={{ width: 28 }}>
                    <span className="sr-only">Expand</span>
                  </th>
                  <th scope="col" style={{ textAlign: "left" }}>Key</th>
                  <th scope="col" style={{ textAlign: "left" }}>Location</th>
                  <th scope="col" style={{ textAlign: "left" }}>Gene / Product</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {shownFeatures.map((feature, idx) => (
                  <FeatureRow
                    key={`${feature.key}-${idx}`}
                    feature={feature}
                    nuccoreUrl={externalNuccoreUrl(accession)}
                    onBlastRange={(range) => {
                      const search = new URLSearchParams({
                        accession,
                        from: String(range.start),
                        to: String(range.stop),
                      });
                      navigate(`/blast/submit?${search.toString()}`);
                    }}
                  />
                ))}
              </tbody>
            </table>
            {!showAllFeatures && visibleFeatures.length > shownFeatures.length && (
              <button
                type="button"
                className="glass-button glass-button--ghost"
                style={{ fontSize: 11, padding: "4px 10px", marginTop: 8 }}
                onClick={() => setShowAllFeatures(true)}
              >
                Show all {visibleFeatures.length.toLocaleString()} features
              </button>
            )}
          </div>
        )}
      </section>

      {genbank && genbank.references.length > 0 && (
        <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 10 }}>
          <h2 style={{ margin: 0, fontSize: 14 }}>References</h2>
          <ol style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 10 }}>
            {genbank.references.map((ref, idx) => (
              <li key={`ref-${idx}`} style={{ fontSize: 12, lineHeight: 1.5 }}>
                {ref.reference && (
                  <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                    REFERENCE {ref.reference}
                  </div>
                )}
                {ref.title && <div style={{ color: "var(--text)" }}>{ref.title}</div>}
                {ref.authors.length > 0 && (
                  <div className="muted">{ref.authors.join(", ")}</div>
                )}
                {ref.consortium && <div className="muted">Consortium: {ref.consortium}</div>}
                {ref.journal && <div className="muted">{ref.journal}</div>}
                {ref.remark && (
                  <div className="muted" style={{ fontStyle: "italic" }}>{ref.remark}</div>
                )}
                <div style={{ display: "flex", flexWrap: "wrap", gap: 12, marginTop: 2 }}>
                  {ref.pubmed && (
                    <a
                      href={`${NCBI_PUBMED_BASE}/${encodeURIComponent(ref.pubmed)}/`}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                    >
                      <ExternalLink size={11} strokeWidth={1.5} />
                      PubMed {ref.pubmed}
                    </a>
                  )}
                  {ref.doi && (
                    <a
                      href={`${DOI_BASE}${encodeURIComponent(ref.doi)}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                    >
                      <ExternalLink size={11} strokeWidth={1.5} />
                      doi:{ref.doi}
                    </a>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </div>
      )}

      <section
        id="sequence-section"
        className="glass-card glass-card--strong"
        style={{ padding: 16, display: "grid", gap: 8 }}
        aria-labelledby="sequence-heading"
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
          <h2 id="sequence-heading" style={{ margin: 0, fontSize: 14 }}>Sequence</h2>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {hasHighlight && (
              <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                Hit range {highlightStart.toLocaleString()}–{highlightStop.toLocaleString()} requested
              </span>
            )}
            {fasta && <CopyButton value={fasta} label="Copy FASTA" title="Copy the full FASTA to the clipboard" />}
          </div>
        </div>
        {fastaQuery.isLoading && <p className="muted" style={{ margin: 0 }}>Loading FASTA…</p>}
        {previewFasta && <SequenceBlocks fasta={previewFasta} highlight={highlightRange} />}
      </section>

      <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 8 }}>
        <h2 style={{ margin: 0, fontSize: 14 }}>
          <Maximize2 size={13} strokeWidth={1.5} style={{ verticalAlign: "-2px", marginRight: 4 }} />
          Advanced view (NCBI Sequence Viewer)
        </h2>
        <p className="muted" style={{ margin: 0, fontSize: 12 }}>
          Pan / zoom the sequence and inspect tracks without leaving the
          dashboard. The interactive viewer is loaded from NCBI only when you
          click below; until then no NCBI script runs and no data leaves your
          browser. The viewer carries the accession
          {hasHighlight ? " and hit range" : ""} only.
        </p>
        {/* Key by accession so navigating to a different record resets the
            embed to its idle (not-yet-loaded) state instead of reusing the
            previous accession's viewer instance. */}
        <SViewerEmbed
          key={accession}
          accession={accession}
          highlight={highlightRange}
          fallbackHref={sviewerEmbedUrl(accession, highlightRange)}
        />
      </div>
    </div>
  );
}


export default SequenceDetail;
