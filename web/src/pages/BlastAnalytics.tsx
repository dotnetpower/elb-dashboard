import { useEffect, useState } from "react";
import { useParams, useSearchParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  BarChart3,
  ArrowLeft,
  RefreshCw,
  Loader2,
  Filter,
  Target,
  Dna,
  TrendingUp,
  Award,
  Layers,
  Eye,
  AlignLeft,
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
} from "lucide-react";

import { blastApi, type BlastHit } from "@/api/endpoints";
import { loadSavedConfig } from "@/components/SetupWizard";

type HitSortBy = "evalue" | "bitscore" | "pident" | "qcovs" | "length";
type HitSortDir = "asc" | "desc";

// ─── Bar chart component (pure CSS, no chart library) ─────────────────

function HorizontalBar({
  label,
  value,
  max,
  color,
}: {
  label: string;
  value: number;
  max: number;
  color: string;
}) {
  const pct = max > 0 ? (value / max) * 100 : 0;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        marginBottom: 4,
        fontSize: 13,
      }}
    >
      <span
        style={{
          minWidth: 110,
          textAlign: "right",
          fontFamily: "var(--font-mono, monospace)",
          color: "var(--text-muted)",
        }}
      >
        {label}
      </span>
      <div
        style={{
          flex: 1,
          height: 18,
          background: "var(--glass-bg)",
          borderRadius: 4,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            width: `${Math.max(pct, 1)}%`,
            height: "100%",
            background: color,
            borderRadius: 4,
            transition: "width 0.3s ease-out",
          }}
        />
      </div>
      <span
        style={{ minWidth: 50, fontFamily: "var(--font-mono, monospace)", fontSize: 12 }}
      >
        {value.toLocaleString()}
      </span>
    </div>
  );
}

// ─── Alignment visualization ──────────────────────────────────────────

const BASE_COLORS: Record<string, string> = {
  A: "#6ad6a3",
  T: "#e07b8a",
  G: "#f0c674",
  C: "#7aa7ff",
  U: "#e07b8a",
  // Amino acids — group by property
  R: "#7aa7ff",
  K: "#7aa7ff",
  H: "#7aa7ff", // positive
  D: "#e07b8a",
  E: "#e07b8a", // negative
  S: "#6ad6a3",
  N: "#6ad6a3",
  Q: "#6ad6a3", // polar
  W: "#f0c674",
  F: "#f0c674",
  Y: "#f0c674", // aromatic
  "-": "#555",
  "*": "#e07b8a",
};

function AlignmentViewer({ hit }: { hit: BlastHit }) {
  const qStart = numberValue(hit.qstart);
  const qEnd = numberValue(hit.qend);
  const sStart = numberValue(hit.sstart);
  const sEnd = numberValue(hit.send);
  const qLen = numberValue(hit.qlen);
  const sLen = numberValue(hit.slen);

  // Build visual alignment bar
  const identityPct = numberValue(hit.pident);

  return (
    <div
      className="glass-card"
      style={{
        padding: 16,
        marginBottom: 12,
        fontSize: 13,
      }}
    >
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
        <div>
          <span style={{ fontWeight: 600 }}>Query:</span>{" "}
          <code className="code-val">{hit.qseqid}</code>
          <span className="muted" style={{ marginLeft: 12 }}>
            {formatRange(hit.qstart, hit.qend)}
            {qLen ? ` / ${qLen}` : ""}
          </span>
        </div>
        <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
          <span
            style={{
              color: identityColor(hit.pident),
            }}
          >
            {formatPercent(hit.pident)} identity
          </span>
          <span className="muted">E={formatEvalue(hit.evalue)}</span>
          <span className="muted">{formatDecimal(hit.bitscore, 1)} bits</span>
        </div>
      </div>

      {/* Subject info */}
      <div style={{ marginBottom: 10 }}>
        <span style={{ fontWeight: 600 }}>Subject:</span>{" "}
        <code className="code-val">{hit.sseqid}</code>
        {hit.stitle && (
          <span className="muted" style={{ marginLeft: 8 }}>
            {hit.stitle}
          </span>
        )}
        <span className="muted" style={{ marginLeft: 12 }}>
          {formatRange(hit.sstart, hit.send)}
          {sLen ? ` / ${sLen}` : ""}
        </span>
      </div>

      {/* Visual alignment bar */}
      <div style={{ margin: "8px 0" }}>
        {/* Query coverage bar */}
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
          <span className="muted" style={{ minWidth: 40, fontSize: 11 }}>
            Query
          </span>
          <div
            style={{
              position: "relative",
              flex: 1,
              height: 16,
              background: "var(--glass-bg)",
              borderRadius: 3,
            }}
          >
            {qStart !== null && qEnd !== null && qLen ? (
              <div
                style={{
                  position: "absolute",
                  left: `${coverageOffset(qStart, qEnd, qLen)}%`,
                  width: `${coverageWidth(qStart, qEnd, qLen)}%`,
                  height: "100%",
                  borderRadius: 3,
                  background: identityColor(hit.pident),
                  opacity: 0.8,
                }}
              />
            ) : (
              <div
                style={{
                  width: `${clampPercent(identityPct ?? 0)}%`,
                  height: "100%",
                  borderRadius: 3,
                  background: "var(--accent)",
                  opacity: 0.8,
                }}
              />
            )}
          </div>
        </div>

        {/* Subject coverage bar */}
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className="muted" style={{ minWidth: 40, fontSize: 11 }}>
            Sbjct
          </span>
          <div
            style={{
              position: "relative",
              flex: 1,
              height: 16,
              background: "var(--glass-bg)",
              borderRadius: 3,
            }}
          >
            {sStart !== null && sEnd !== null && sLen ? (
              <div
                style={{
                  position: "absolute",
                  left: `${coverageOffset(sStart, sEnd, sLen)}%`,
                  width: `${coverageWidth(sStart, sEnd, sLen)}%`,
                  height: "100%",
                  borderRadius: 3,
                  background: identityColor(hit.pident),
                  opacity: 0.6,
                }}
              />
            ) : (
              <div
                style={{
                  width: `${clampPercent(identityPct ?? 0)}%`,
                  height: "100%",
                  borderRadius: 3,
                  background: "var(--accent)",
                  opacity: 0.6,
                }}
              />
            )}
          </div>
        </div>
      </div>

      {/* Sequence alignment (if available) */}
      {hit.qseq && hit.sseq && qStart !== null && sStart !== null && (
        <div style={{ marginTop: 12, overflowX: "auto" }}>
          <SequenceAlignment
            qseq={hit.qseq}
            sseq={hit.sseq}
            qstart={qStart}
            sstart={sStart}
          />
        </div>
      )}

      {/* Stats row */}
      <div
        style={{
          display: "flex",
          gap: 20,
          marginTop: 10,
          fontSize: 12,
          color: "var(--text-muted)",
        }}
      >
        <span>Length: {formatInteger(hit.length)}</span>
        <span>Mismatches: {formatInteger(hit.mismatch)}</span>
        <span>Gaps: {formatInteger(hit.gaps ?? hit.gapopen)}</span>
        {hit.ppos !== undefined && <span>Positives: {formatPercent(hit.ppos)}</span>}
      </div>
    </div>
  );
}

function BlastHitsTable({ hits }: { hits: BlastHit[] }) {
  return (
    <div className="glass-card" style={{ padding: 16, overflowX: "auto" }}>
      <table className="table" style={{ width: "100%", minWidth: 1160, fontSize: 13 }}>
        <thead>
          <tr>
            <th style={{ textAlign: "left" }}>Review</th>
            <th style={{ textAlign: "left" }}>Query</th>
            <th style={{ textAlign: "left" }}>Accession</th>
            <th style={{ textAlign: "left" }}>Organism</th>
            <th style={{ textAlign: "left" }}>Description</th>
            <th style={{ textAlign: "right" }}>HSP Cover</th>
            <th style={{ textAlign: "right" }}>% Identity</th>
            <th style={{ textAlign: "right" }}>Length</th>
            <th style={{ textAlign: "right" }}>E-value</th>
            <th style={{ textAlign: "right" }}>Bit Score</th>
            <th style={{ textAlign: "right" }}>Query Range</th>
            <th style={{ textAlign: "left" }}>Source</th>
          </tr>
        </thead>
        <tbody>
          {hits.map((hit, index) => (
            <tr key={`${hit.qseqid}-${hit.sseqid}-${hit.qstart}-${hit.sstart}-${index}`}>
              <td>
                <ReviewBadge hit={hit} />
              </td>
              <td style={{ fontFamily: "var(--font-mono, monospace)", maxWidth: 150 }}>
                {hit.qseqid}
              </td>
              <td style={{ fontFamily: "var(--font-mono, monospace)", maxWidth: 150 }}>
                <a
                  href={ncbiSearchUrl(hit.sseqid)}
                  target="_blank"
                  rel="noreferrer"
                  style={{ color: "var(--accent)", display: "inline-flex", gap: 4 }}
                >
                  {hit.sseqid}
                  <ExternalLink size={12} strokeWidth={1.5} />
                </a>
              </td>
              <td style={{ maxWidth: 180, color: "var(--text-muted)" }}>
                {hit.sscinames || taxidLabel(hit.staxids) || "—"}
              </td>
              <td style={{ maxWidth: 280, color: "var(--text-muted)" }}>
                {hit.stitle || "—"}
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
              <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                {formatDecimal(hit.bitscore, 1)}
              </td>
              <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                {formatRange(hit.qstart, hit.qend)}
              </td>
              <td style={{ maxWidth: 180, color: "var(--text-muted)", fontSize: 12 }}>
                {shortBlobName(hit.source_blob)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SequenceAlignment({
  qseq,
  sseq,
  qstart,
  sstart,
}: {
  qseq: string;
  sseq: string;
  qstart: number;
  sstart: number;
}) {
  // Display in blocks of 60 characters
  const blockSize = 60;
  const blocks: Array<{ q: string; m: string; s: string; qpos: number; spos: number }> =
    [];

  for (let i = 0; i < qseq.length; i += blockSize) {
    const qBlock = qseq.slice(i, i + blockSize);
    const sBlock = sseq.slice(i, i + blockSize);
    // Build match line
    let matchLine = "";
    for (let j = 0; j < qBlock.length; j++) {
      if (qBlock[j] === sBlock[j]) matchLine += "|";
      else if (qBlock[j] !== "-" && sBlock[j] !== "-") matchLine += ":";
      else matchLine += " ";
    }
    blocks.push({
      q: qBlock,
      m: matchLine,
      s: sBlock,
      qpos: qstart + i,
      spos: sstart + i,
    });
  }

  return (
    <div
      style={{ fontFamily: "var(--font-mono, monospace)", fontSize: 12, lineHeight: 1.6 }}
    >
      {blocks.map((block, idx) => (
        <div key={idx} style={{ marginBottom: 8 }}>
          <div style={{ display: "flex" }}>
            <span
              className="muted"
              style={{ minWidth: 60, textAlign: "right", marginRight: 8 }}
            >
              Q {block.qpos}
            </span>
            <span>
              {block.q.split("").map((ch, ci) => (
                <span
                  key={ci}
                  style={{
                    color: BASE_COLORS[ch.toUpperCase()] ?? "var(--text-primary)",
                  }}
                >
                  {ch}
                </span>
              ))}
            </span>
          </div>
          <div style={{ display: "flex" }}>
            <span style={{ minWidth: 60, marginRight: 8 }} />
            <span style={{ color: "var(--text-muted)" }}>{block.m}</span>
          </div>
          <div style={{ display: "flex" }}>
            <span
              className="muted"
              style={{ minWidth: 60, textAlign: "right", marginRight: 8 }}
            >
              S {block.spos}
            </span>
            <span>
              {block.s.split("").map((ch, ci) => (
                <span
                  key={ci}
                  style={{
                    color: BASE_COLORS[ch.toUpperCase()] ?? "var(--text-primary)",
                  }}
                >
                  {ch}
                </span>
              ))}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Main page ────────────────────────────────────────────────────────

export function BlastAnalytics() {
  const { jobId } = useParams<{ jobId: string }>();
  const [searchParams] = useSearchParams();
  const cfg = loadSavedConfig();

  const subscriptionId = searchParams.get("subscription_id") ?? cfg?.subscriptionId ?? "";
  const storageAccount =
    searchParams.get("storage_account") ?? cfg?.storageAccountName ?? "";

  const [activeTab, setActiveTab] = useState<"hits" | "overview" | "alignments">("hits");
  const [queryFilter, setQueryFilter] = useState<string>("");
  const [subjectFilter, setSubjectFilter] = useState<string>("");
  const [organismFilter, setOrganismFilter] = useState<string>("");
  const [minIdentity, setMinIdentity] = useState<number>(0);
  const [minQueryCover, setMinQueryCover] = useState<number>(0);
  const [maxEvalue, setMaxEvalue] = useState<number>(10);
  const [sortBy, setSortBy] = useState<HitSortBy>("evalue");
  const [sortDir, setSortDir] = useState<HitSortDir>("asc");
  const [page, setPage] = useState<number>(1);
  const pageSize = 100;

  // Fetch aggregate stats
  const statsQuery = useQuery({
    queryKey: ["blast-aggregate", jobId, subscriptionId, storageAccount],
    queryFn: () => blastApi.resultsAggregate(jobId!, subscriptionId, storageAccount),
    enabled: !!jobId && !!subscriptionId && !!storageAccount,
    staleTime: 60_000,
  });

  // Fetch alignments
  const alignQuery = useQuery({
    queryKey: [
      "blast-alignments",
      jobId,
      subscriptionId,
      storageAccount,
      queryFilter,
      subjectFilter,
      organismFilter,
      minIdentity,
      minQueryCover,
      maxEvalue,
      sortBy,
      sortDir,
      page,
    ],
    queryFn: () =>
      blastApi.resultsAlignments(jobId!, subscriptionId, storageAccount, {
        page,
        page_size: pageSize,
        query_id: queryFilter || undefined,
        subject_id: subjectFilter || undefined,
        organism: organismFilter || undefined,
        min_identity: minIdentity,
        min_query_cover: minQueryCover,
        max_evalue: maxEvalue,
        sort_by: sortBy,
        sort_dir: sortDir,
      }),
    enabled: !!jobId && !!subscriptionId && !!storageAccount && activeTab !== "overview",
    staleTime: 60_000,
  });

  const stats = statsQuery.data?.stats;
  const alignments = alignQuery.data?.alignments ?? [];
  const queryIds = alignQuery.data?.query_ids ?? [];
  const pageCount = alignQuery.data?.pages ?? 0;
  const filteredHitCount =
    alignQuery.data?.filtered_hits ?? alignQuery.data?.total_hits ?? 0;

  useEffect(() => {
    if (pageCount > 0 && page > pageCount) {
      setPage(pageCount);
    }
  }, [page, pageCount]);

  const resetPagedFilter = (callback: () => void) => {
    callback();
    setPage(1);
  };

  // Compute max for chart scaling
  const evalueMax = stats ? Math.max(...Object.values(stats.evalue_distribution)) : 0;
  const identMax = stats ? Math.max(...Object.values(stats.identity_distribution)) : 0;

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 24 }}>
        <Link
          to={`/blast/jobs/${jobId}`}
          className="btn btn--ghost btn--sm"
          style={{ padding: "6px 8px" }}
        >
          <ArrowLeft size={16} />
        </Link>
        <BarChart3 size={22} strokeWidth={1.5} style={{ color: "var(--accent)" }} />
        <h1 style={{ margin: 0, fontSize: 22 }}>Results Analytics</h1>
        <span className="muted" style={{ fontSize: 13 }}>
          Job: {jobId}
        </span>
      </div>

      {/* Tabs */}
      <div style={{ display: "flex", gap: 4, marginBottom: 24 }}>
        <button
          className={`btn btn--sm ${activeTab === "hits" ? "btn--primary" : "btn--ghost"}`}
          onClick={() => setActiveTab("hits")}
        >
          <Eye size={14} /> Hits
        </button>
        <button
          className={`btn btn--sm ${activeTab === "overview" ? "btn--primary" : "btn--ghost"}`}
          onClick={() => setActiveTab("overview")}
        >
          <BarChart3 size={14} /> Overview
        </button>
        <button
          className={`btn btn--sm ${activeTab === "alignments" ? "btn--primary" : "btn--ghost"}`}
          onClick={() => setActiveTab("alignments")}
        >
          <AlignLeft size={14} /> Alignments
        </button>
      </div>

      {activeTab === "hits" && (
        <>
          <ResultFilterBar
            queryFilter={queryFilter}
            subjectFilter={subjectFilter}
            organismFilter={organismFilter}
            minIdentity={minIdentity}
            minQueryCover={minQueryCover}
            maxEvalue={maxEvalue}
            sortBy={sortBy}
            sortDir={sortDir}
            queryIds={queryIds}
            alignQueryData={alignQuery.data}
            isFetching={alignQuery.isFetching}
            page={page}
            pageCount={pageCount}
            onQueryFilterChange={(value) => resetPagedFilter(() => setQueryFilter(value))}
            onSubjectFilterChange={(value) =>
              resetPagedFilter(() => setSubjectFilter(value))
            }
            onOrganismFilterChange={(value) =>
              resetPagedFilter(() => setOrganismFilter(value))
            }
            onMinIdentityChange={(value) => resetPagedFilter(() => setMinIdentity(value))}
            onMinQueryCoverChange={(value) =>
              resetPagedFilter(() => setMinQueryCover(value))
            }
            onMaxEvalueChange={(value) => resetPagedFilter(() => setMaxEvalue(value))}
            onSortByChange={(value) => resetPagedFilter(() => setSortBy(value))}
            onSortDirChange={(value) => resetPagedFilter(() => setSortDir(value))}
            onPageChange={setPage}
            onRefresh={() => alignQuery.refetch()}
          />

          {alignQuery.isLoading && (
            <div className="glass-card" style={{ padding: 40, textAlign: "center" }}>
              <Loader2 size={24} className="spin" style={{ color: "var(--accent)" }} />
              <p className="muted" style={{ marginTop: 12 }}>
                Loading BLAST hits...
              </p>
            </div>
          )}

          {alignQuery.isError && (
            <div
              className="glass-card"
              style={{ padding: 20, borderColor: "var(--danger)" }}
            >
              <p style={{ color: "var(--danger)" }}>
                Failed: {(alignQuery.error as Error).message}
              </p>
            </div>
          )}

          {isPartialResult(alignQuery.data) && <DegradedBanner data={alignQuery.data} />}

          {alignments.length === 0 &&
            !alignQuery.isLoading &&
            !isPartialResult(alignQuery.data) &&
            filteredHitCount === 0 && (
              <div className="glass-card" style={{ padding: 24, textAlign: "center" }}>
                <Eye size={32} className="muted" style={{ marginBottom: 8 }} />
                <p className="muted">No hits found</p>
              </div>
            )}

          {alignments.length > 0 && <BlastHitsTable hits={alignments} />}
        </>
      )}

      {activeTab === "overview" && (
        <>
          {statsQuery.isLoading && (
            <div className="glass-card" style={{ padding: 40, textAlign: "center" }}>
              <Loader2 size={24} className="spin" style={{ color: "var(--accent)" }} />
              <p className="muted" style={{ marginTop: 12 }}>
                Parsing BLAST results...
              </p>
            </div>
          )}

          {statsQuery.isError && (
            <div
              className="glass-card"
              style={{ padding: 20, borderColor: "var(--danger)" }}
            >
              <p style={{ color: "var(--danger)" }}>
                Failed to load results: {(statsQuery.error as Error).message}
              </p>
            </div>
          )}

          {/* Backend tells us when result blobs were unreadable or the
              file set was trimmed by the safety cap. Surface both so the
              researcher knows the analytics are partial / unreliable
              instead of trusting an apparently-empty hit set. */}
          {statsQuery.data && (statsQuery.data.degraded || statsQuery.data.truncated) && (
            <DegradedBanner data={statsQuery.data} />
          )}

          {stats && (
            <>
              {/* Summary cards */}
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
                  gap: 16,
                  marginBottom: 24,
                }}
              >
                <StatCard
                  icon={<Target size={18} />}
                  label="Total Hits"
                  value={stats.total_hits.toLocaleString()}
                />
                <StatCard
                  icon={<Dna size={18} />}
                  label="Unique Queries"
                  value={stats.unique_queries.toLocaleString()}
                />
                <StatCard
                  icon={<Layers size={18} />}
                  label="Unique Subjects"
                  value={stats.unique_subjects.toLocaleString()}
                />
                <StatCard
                  icon={<TrendingUp size={18} />}
                  label="Avg Identity"
                  value={stats.avg_identity ? `${stats.avg_identity}%` : "—"}
                  accent
                />
                <StatCard
                  icon={<Award size={18} />}
                  label="Avg Bit Score"
                  value={stats.avg_bitscore?.toFixed(1) ?? "—"}
                />
                <StatCard
                  icon={<Award size={18} />}
                  label="Best E-value"
                  value={stats.min_evalue !== null ? formatEvalue(stats.min_evalue) : "—"}
                />
              </div>

              {/* E-value distribution */}
              <div className="glass-card" style={{ padding: 20, marginBottom: 20 }}>
                <h3 style={{ margin: "0 0 16px", fontSize: 15 }}>E-value Distribution</h3>
                {Object.entries(stats.evalue_distribution).map(([bin, count]) => (
                  <HorizontalBar
                    key={bin}
                    label={bin}
                    value={count}
                    max={evalueMax}
                    color="var(--accent)"
                  />
                ))}
              </div>

              {/* Identity distribution */}
              <div className="glass-card" style={{ padding: 20, marginBottom: 20 }}>
                <h3 style={{ margin: "0 0 16px", fontSize: 15 }}>
                  Identity % Distribution
                </h3>
                {Object.entries(stats.identity_distribution).map(([bin, count]) => {
                  const pct = parseInt(bin);
                  const color =
                    pct >= 90
                      ? "var(--success)"
                      : pct >= 70
                        ? "var(--warning)"
                        : pct >= 50
                          ? "var(--accent)"
                          : "var(--danger)";
                  return (
                    <HorizontalBar
                      key={bin}
                      label={bin}
                      value={count}
                      max={identMax}
                      color={color}
                    />
                  );
                })}
              </div>

              {/* Top hit subjects */}
              <div className="glass-card" style={{ padding: 20, marginBottom: 20 }}>
                <h3 style={{ margin: "0 0 16px", fontSize: 15 }}>Top Hit Subjects</h3>
                {stats.top_subjects.length === 0 ? (
                  <p className="muted">No subject data</p>
                ) : (
                  <div style={{ overflowX: "auto" }}>
                    <table className="table" style={{ width: "100%", fontSize: 13 }}>
                      <thead>
                        <tr>
                          <th>#</th>
                          <th>Subject ID</th>
                          <th>Hit Count</th>
                          <th style={{ width: 200 }}>Distribution</th>
                        </tr>
                      </thead>
                      <tbody>
                        {stats.top_subjects.map((s, i) => (
                          <tr key={s.id}>
                            <td className="muted">{i + 1}</td>
                            <td style={{ fontFamily: "var(--font-mono, monospace)" }}>
                              {s.id}
                            </td>
                            <td>{s.count}</td>
                            <td>
                              <div
                                style={{
                                  width: "100%",
                                  height: 12,
                                  background: "var(--glass-bg)",
                                  borderRadius: 3,
                                }}
                              >
                                <div
                                  style={{
                                    width: `${(s.count / stats.top_subjects[0].count) * 100}%`,
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
                )}
              </div>

              {/* Files parsed info */}
              {stats.files_parsed !== undefined && (
                <p className="muted" style={{ fontSize: 12, textAlign: "center" }}>
                  Parsed {stats.files_parsed} of {stats.total_files} result file
                  {stats.total_files !== 1 ? "s" : ""}
                </p>
              )}
            </>
          )}
        </>
      )}

      {activeTab === "alignments" && (
        <>
          {/* Filter bar */}
          <ResultFilterBar
            queryFilter={queryFilter}
            subjectFilter={subjectFilter}
            organismFilter={organismFilter}
            minIdentity={minIdentity}
            minQueryCover={minQueryCover}
            maxEvalue={maxEvalue}
            sortBy={sortBy}
            sortDir={sortDir}
            queryIds={queryIds}
            alignQueryData={alignQuery.data}
            isFetching={alignQuery.isFetching}
            page={page}
            pageCount={pageCount}
            onQueryFilterChange={(value) => resetPagedFilter(() => setQueryFilter(value))}
            onSubjectFilterChange={(value) =>
              resetPagedFilter(() => setSubjectFilter(value))
            }
            onOrganismFilterChange={(value) =>
              resetPagedFilter(() => setOrganismFilter(value))
            }
            onMinIdentityChange={(value) => resetPagedFilter(() => setMinIdentity(value))}
            onMinQueryCoverChange={(value) =>
              resetPagedFilter(() => setMinQueryCover(value))
            }
            onMaxEvalueChange={(value) => resetPagedFilter(() => setMaxEvalue(value))}
            onSortByChange={(value) => resetPagedFilter(() => setSortBy(value))}
            onSortDirChange={(value) => resetPagedFilter(() => setSortDir(value))}
            onPageChange={setPage}
            onRefresh={() => alignQuery.refetch()}
          />

          {alignQuery.isLoading && (
            <div className="glass-card" style={{ padding: 40, textAlign: "center" }}>
              <Loader2 size={24} className="spin" style={{ color: "var(--accent)" }} />
              <p className="muted" style={{ marginTop: 12 }}>
                Loading alignments...
              </p>
            </div>
          )}

          {alignQuery.isError && (
            <div
              className="glass-card"
              style={{ padding: 20, borderColor: "var(--danger)" }}
            >
              <p style={{ color: "var(--danger)" }}>
                Failed: {(alignQuery.error as Error).message}
              </p>
            </div>
          )}

          {isPartialResult(alignQuery.data) && <DegradedBanner data={alignQuery.data} />}

          {alignments.length === 0 &&
            !alignQuery.isLoading &&
            !isPartialResult(alignQuery.data) &&
            filteredHitCount === 0 && (
              <div className="glass-card" style={{ padding: 24, textAlign: "center" }}>
                <Eye size={32} className="muted" style={{ marginBottom: 8 }} />
                <p className="muted">No alignments found</p>
              </div>
            )}

          {alignments.map((hit, i) => (
            <AlignmentViewer key={`${hit.qseqid}-${hit.sseqid}-${i}`} hit={hit} />
          ))}
        </>
      )}
    </div>
  );
}

// ─── Helpers ──────────────────────────────────────────────────────────

function ResultFilterBar({
  queryFilter,
  subjectFilter,
  organismFilter,
  minIdentity,
  minQueryCover,
  maxEvalue,
  sortBy,
  sortDir,
  queryIds,
  alignQueryData,
  isFetching,
  page,
  pageCount,
  onQueryFilterChange,
  onSubjectFilterChange,
  onOrganismFilterChange,
  onMinIdentityChange,
  onMinQueryCoverChange,
  onMaxEvalueChange,
  onSortByChange,
  onSortDirChange,
  onPageChange,
  onRefresh,
}: {
  queryFilter: string;
  subjectFilter: string;
  organismFilter: string;
  minIdentity: number;
  minQueryCover: number;
  maxEvalue: number;
  sortBy: HitSortBy;
  sortDir: HitSortDir;
  queryIds: string[];
  alignQueryData?: {
    returned: number;
    total_hits: number;
    filtered_hits?: number;
    files_parsed?: number;
    total_files?: number;
    read_failures?: number;
    truncated?: boolean;
    hit_limit_reached?: boolean;
  };
  isFetching: boolean;
  page: number;
  pageCount: number;
  onQueryFilterChange: (value: string) => void;
  onSubjectFilterChange: (value: string) => void;
  onOrganismFilterChange: (value: string) => void;
  onMinIdentityChange: (value: number) => void;
  onMinQueryCoverChange: (value: number) => void;
  onMaxEvalueChange: (value: number) => void;
  onSortByChange: (value: HitSortBy) => void;
  onSortDirChange: (value: HitSortDir) => void;
  onPageChange: (value: number) => void;
  onRefresh: () => void;
}) {
  const filteredHits = alignQueryData?.filtered_hits ?? alignQueryData?.total_hits ?? 0;
  const totalHits = alignQueryData?.total_hits ?? 0;
  return (
    <div
      className="glass-card"
      style={{
        padding: 12,
        marginBottom: 16,
        display: "flex",
        alignItems: "center",
        flexWrap: "wrap",
        gap: 12,
      }}
    >
      <Filter size={14} className="muted" />
      <select
        className="form-input"
        style={{ width: 220, fontSize: 13 }}
        value={queryFilter}
        onChange={(e) => onQueryFilterChange(e.target.value)}
      >
        <option value="">All queries</option>
        {queryIds.map((qid) => (
          <option key={qid} value={qid}>
            {qid}
          </option>
        ))}
      </select>
      <input
        className="form-input"
        style={{ width: 150, fontSize: 13 }}
        value={subjectFilter}
        placeholder="Accession"
        onChange={(event) => onSubjectFilterChange(event.target.value)}
      />
      <input
        className="form-input"
        style={{ width: 170, fontSize: 13 }}
        value={organismFilter}
        placeholder="Organism or taxid"
        onChange={(event) => onOrganismFilterChange(event.target.value)}
      />
      <label className="muted" style={{ display: "flex", alignItems: "center", gap: 6 }}>
        Identity
        <input
          className="form-input"
          type="number"
          min={0}
          max={100}
          step={1}
          style={{ width: 72, fontSize: 13 }}
          value={minIdentity}
          onChange={(event) => onMinIdentityChange(parsePercentInput(event.target.value))}
        />
      </label>
      <label className="muted" style={{ display: "flex", alignItems: "center", gap: 6 }}>
        HSP cover
        <input
          className="form-input"
          type="number"
          min={0}
          max={100}
          step={1}
          style={{ width: 72, fontSize: 13 }}
          value={minQueryCover}
          onChange={(event) =>
            onMinQueryCoverChange(parsePercentInput(event.target.value))
          }
        />
      </label>
      <label className="muted" style={{ display: "flex", alignItems: "center", gap: 6 }}>
        Max E
        <input
          className="form-input"
          type="number"
          min={0}
          step="any"
          style={{ width: 86, fontSize: 13 }}
          value={maxEvalue}
          onChange={(event) =>
            onMaxEvalueChange(parseNonNegativeInput(event.target.value, 10))
          }
        />
      </label>
      <select
        className="form-input"
        style={{ width: 130, fontSize: 13 }}
        value={sortBy}
        onChange={(event) => onSortByChange(event.target.value as HitSortBy)}
      >
        <option value="evalue">E-value</option>
        <option value="bitscore">Bit score</option>
        <option value="pident">Identity</option>
        <option value="qcovs">HSP cover</option>
        <option value="length">Length</option>
      </select>
      <button
        className="btn btn--ghost btn--sm"
        onClick={() => onSortDirChange(sortDir === "asc" ? "desc" : "asc")}
      >
        {sortDir === "asc" ? "Asc" : "Desc"}
      </button>
      <span className="muted" style={{ fontSize: 13 }}>
        {alignQueryData
          ? `${alignQueryData.returned} shown, ${filteredHits.toLocaleString()} filtered of ${totalHits.toLocaleString()} hits`
          : ""}
      </span>
      {alignQueryData?.files_parsed !== undefined && (
        <span className="muted" style={{ fontSize: 12 }}>
          {alignQueryData.files_parsed} / {alignQueryData.total_files ?? 0} files
        </span>
      )}
      <button
        className="btn btn--ghost btn--sm"
        onClick={() => onPageChange(Math.max(1, page - 1))}
        disabled={page <= 1 || isFetching}
      >
        <ChevronLeft size={14} />
      </button>
      <span className="muted" style={{ fontSize: 13 }}>
        Page {pageCount ? page : 0} / {pageCount}
      </span>
      <button
        className="btn btn--ghost btn--sm"
        onClick={() => onPageChange(Math.min(pageCount, page + 1))}
        disabled={!pageCount || page >= pageCount || isFetching}
      >
        <ChevronRight size={14} />
      </button>
      <button
        className="btn btn--ghost btn--sm"
        onClick={onRefresh}
        disabled={isFetching}
      >
        <RefreshCw size={14} className={isFetching ? "spin" : ""} />
      </button>
    </div>
  );
}

function numberValue(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string") return null;
  const parsed = Number(value.trim());
  return Number.isFinite(parsed) ? parsed : null;
}

function formatDecimal(value: unknown, digits: number): string {
  const numeric = numberValue(value);
  return numeric === null ? "—" : numeric.toFixed(digits);
}

function formatInteger(value: unknown): string {
  const numeric = numberValue(value);
  return numeric === null ? "—" : Math.round(numeric).toLocaleString();
}

function formatPercent(value: unknown): string {
  const numeric = numberValue(value);
  return numeric === null ? "—" : `${numeric.toFixed(1)}%`;
}

function formatRange(start: unknown, end: unknown): string {
  const startNumber = numberValue(start);
  const endNumber = numberValue(end);
  if (startNumber === null || endNumber === null) return "—";
  return `${Math.round(startNumber)}–${Math.round(endNumber)}`;
}

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, value));
}

function coverageOffset(start: number, end: number, total: number): number {
  if (total <= 0) return 0;
  return clampPercent(((Math.min(start, end) - 1) / total) * 100);
}

function coverageWidth(start: number, end: number, total: number): number {
  if (total <= 0) return 0;
  return clampPercent(((Math.abs(end - start) + 1) / total) * 100);
}

function identityColor(value: unknown): string {
  const numeric = numberValue(value);
  if (numeric === null) return "var(--text-muted)";
  if (numeric >= 90) return "var(--success)";
  if (numeric >= 70) return "var(--warning)";
  return "var(--danger)";
}

function isPartialResult(
  data:
    | {
        degraded?: boolean;
        truncated?: boolean;
        hit_limit_reached?: boolean;
        read_failures?: number;
      }
    | undefined,
): data is NonNullable<typeof data> {
  return Boolean(
    data?.degraded ||
    data?.truncated ||
    data?.hit_limit_reached ||
    (data?.read_failures ?? 0) > 0,
  );
}

function parsePercentInput(value: string): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0;
  return Math.max(0, Math.min(100, numeric));
}

function parseNonNegativeInput(value: string, fallback: number): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return fallback;
  return Math.max(0, numeric);
}

function ncbiSearchUrl(accession: string): string {
  return `https://www.ncbi.nlm.nih.gov/search/all/?term=${encodeURIComponent(accession)}`;
}

function taxidLabel(value: string | undefined): string {
  if (!value) return "";
  return value
    .split(";")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => `taxid:${part}`)
    .join(", ");
}

function shortBlobName(value: string | undefined): string {
  if (!value) return "—";
  const parts = value.split("/").filter(Boolean);
  return parts.at(-1) ?? value;
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

function StatCard({
  icon,
  label,
  value,
  accent,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  accent?: boolean;
}) {
  return (
    <div className="glass-card" style={{ padding: 16, textAlign: "center" }}>
      <div
        style={{ color: accent ? "var(--accent)" : "var(--text-muted)", marginBottom: 6 }}
      >
        {icon}
      </div>
      <div
        style={{
          fontSize: 22,
          fontWeight: 700,
          color: accent ? "var(--accent)" : "var(--text-primary)",
        }}
      >
        {value}
      </div>
      <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
        {label}
      </div>
    </div>
  );
}

const DEGRADED_REASON_LABEL: Record<string, string> = {
  all_reads_failed:
    "Every result file failed to download. RBAC, network outage, or the storage account is unreachable.",
  aggregation_failed:
    "Hits were retrieved but the analytics aggregation crashed. Try refreshing; if it persists, the data shape may be unexpected.",
  no_results: "The job finished but no output blobs were produced.",
};

function DegradedBanner({
  data,
}: {
  data: {
    degraded?: boolean;
    degraded_reason?: string;
    message?: string;
    files_parsed?: number;
    total_files?: number;
    read_failures?: number;
    truncated?: boolean;
    hit_limit_reached?: boolean;
  };
}) {
  const isError = Boolean(data.degraded);
  const colour = isError ? "var(--danger)" : "var(--warning)";
  const reasonText =
    (data.degraded_reason && DEGRADED_REASON_LABEL[data.degraded_reason]) ||
    data.message ||
    data.degraded_reason ||
    null;
  return (
    <div
      className="glass-card"
      style={{
        padding: 16,
        marginBottom: 20,
        borderColor: colour,
        borderWidth: 1,
        borderStyle: "solid",
      }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
        <AlertTriangle
          size={18}
          strokeWidth={1.5}
          style={{ color: colour, marginTop: 2, flexShrink: 0 }}
        />
        <div style={{ flex: 1 }}>
          <div style={{ color: colour, fontWeight: 600, marginBottom: 4 }}>
            {isError ? "Results are degraded" : "Results are partial"}
          </div>
          {reasonText && (
            <div style={{ fontSize: 13, color: "var(--text-primary)", marginBottom: 6 }}>
              {reasonText}
            </div>
          )}
          <div className="muted" style={{ fontSize: 12 }}>
            {typeof data.files_parsed === "number" &&
              typeof data.total_files === "number" && (
                <span>
                  Parsed {data.files_parsed.toLocaleString()} of{" "}
                  {data.total_files.toLocaleString()} result file
                  {data.total_files === 1 ? "" : "s"}.{" "}
                </span>
              )}
            {typeof data.read_failures === "number" && data.read_failures > 0 && (
              <span>
                {data.read_failures.toLocaleString()} read failure
                {data.read_failures === 1 ? "" : "s"}.{" "}
              </span>
            )}
            {data.truncated && (
              <span>
                Showing the first batch only — re-run with fewer query splits for full
                coverage.
              </span>
            )}
            {data.hit_limit_reached && (
              <span>
                {" "}
                Hit review stopped at the safety cap; export the raw files for full
                coverage.
              </span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function formatEvalue(value: unknown): string {
  const ev = numberValue(value);
  if (ev === null) return "—";
  if (ev === 0) return "0";
  if (ev < 1e-100) return ev.toExponential(0);
  if (ev < 0.01) return ev.toExponential(1);
  if (ev < 1) return ev.toFixed(3);
  return ev.toFixed(1);
}
