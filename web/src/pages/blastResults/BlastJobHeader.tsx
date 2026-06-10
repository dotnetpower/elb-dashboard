import { useEffect, useRef, useState } from "react";
import { useTransientState } from "../../hooks/useTransientState";
import { Link, useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  CheckCircle2,
  ChevronDown,
  Clock,
  Copy,
  Download,
  Edit3,
  Loader2,
  StopCircle,
} from "lucide-react";

import { ElapsedTimer } from "@/components/BlastFilePreview";
import { useToast } from "@/components/Toast";
import type {
  BlastDatabaseMetadata,
  BlastExportFormat,
  BlastResultFile,
} from "@/api/endpoints";
import { blastApi } from "@/api/blast";
import { BlastHelpMenu } from "@/pages/blastResults/BlastHelpMenu";
import {
  buildConfigFilename,
  downloadConfigJson,
  partialFormFromJobPayload,
  PENDING_DUPLICATE_KEY,
  serializeFormToConfig,
  type BlastConfigSnapshot,
  type ExportableFormFields,
} from "@/pages/blastSubmit/configSerializer";
import { INITIAL, type FormState } from "@/pages/blastSubmitModel";

interface BlastJobHeaderProps {
  jobId: string;
  jobTitle: string | null;
  createdAt: string | null;
  updatedAt?: string | null;
  isRunning: boolean;
  canCancel: boolean;
  cancelDisabled: boolean;
  onRequestCancel: () => void;
  /** Original submit payload, used by Edit search / Duplicate / Export config. */
  jobPayload?: Record<string, unknown> | undefined;
  /** NCBI-style metadata surfaced in the new 7-line header. */
  program?: string | null;
  database?: string | null;
  customStatus?: unknown;
  databaseMetadata?: BlastDatabaseMetadata | null;
  configSnapshot?: Record<string, unknown> | undefined;
  infrastructure?: Record<string, unknown> | undefined;
  /** Hooks for the "Download all results" combo. */
  exportingFormat: BlastExportFormat | null;
  onExport: (format: BlastExportFormat) => void;
  hasExportTargets: boolean;
  resultFiles?: BlastResultFile[];
}

/**
 * NCBI-styled BLAST search header.
 *
 * Layout (mirrors NCBI Web BLAST):
 *   < Recent searches                                        [Help links]
 *   [Search title]                              [Cancel] [Edit search] [Duplicate]
 *   Search ID: …    Created: …    [Copy] [Download all ▾]
 *   Program: blastn   Database: …   Query: …   Molecule: dna   Query length: …
 *
 * Operational metadata (cluster, region, storage) lives on the
 * dedicated "Run details" tab — keeping this header focused on what
 * a researcher needs at a glance to confirm "yes, this is my search".
 */
export function BlastJobHeader({
  jobId,
  jobTitle,
  createdAt,
  updatedAt,
  isRunning,
  canCancel,
  cancelDisabled,
  onRequestCancel,
  jobPayload,
  program,
  database,
  customStatus,
  databaseMetadata,
  configSnapshot,
  infrastructure,
  exportingFormat,
  onExport,
  hasExportTargets,
  resultFiles = [],
}: BlastJobHeaderProps) {
  const navigate = useNavigate();
  const { toast } = useToast();
  const [copiedId, flashCopiedId] = useTransientState(false);
  const [showDownloadMenu, setShowDownloadMenu] = useState(false);
  const [loadingQuery, setLoadingQuery] = useState(false);
  const [copyingCitation, setCopyingCitation] = useState(false);

  const hydratableFields = jobPayload ? partialFormFromJobPayload(jobPayload) : null;
  const canReuseConfig = Boolean(hydratableFields);

  const queryId =
    pickString(jobPayload, ["query_id", "query_name", "query_label"]) ??
    firstQueryRecordId(jobPayload) ??
    queryFileBasename(jobPayload);
  const queryDescription =
    pickString(jobPayload, ["query_description", "description"]) ?? jobTitle ?? null;
  const molecule = pickString(jobPayload, ["molecule_type"]) ?? deriveMolecule(program);
  const queryLength =
    pickNumber(jobPayload, ["query_length"]) ?? firstQueryRecordLength(jobPayload);
  const submittedAt = createdAt ? new Date(createdAt) : null;

  const cluster = pickString(infrastructure, ["cluster_name"]);
  const region = pickString(infrastructure, ["region"]);
  const evalue = pickString(configSnapshot, ["evalue"]);
  const maxTargets = pickString(configSnapshot, ["max_target_seqs"]);
  const submittedOutfmt =
    pickNumber(configSnapshot, ["outfmt"]) ?? pickNumber(jobPayload, ["outfmt"]);
  const dbSequenceCount = formatCount(databaseMetadata?.number_of_sequences);
  const dbLetterCount = formatCount(databaseMetadata?.number_of_letters);
  const timingMetrics = buildTimingMetrics({ createdAt, updatedAt, customStatus });

  const handleCopyId = async () => {
    try {
      await navigator.clipboard.writeText(jobId);
      flashCopiedId(true, 1500);
    } catch {
      toast("Failed to copy search ID", "error");
    }
  };

  const handleCopyCitation = async () => {
    if (copyingCitation) return;
    setCopyingCitation(true);
    try {
      const citation = await blastApi.getCitation(jobId, "text");
      await navigator.clipboard.writeText(citation.citation);
      toast("Methods citation copied to clipboard", "success");
    } catch {
      toast("Citation is not available for this search yet", "error");
    } finally {
      setCopyingCitation(false);
    }
  };

  const handleEditSearch = async () => {
    if (!hydratableFields) return;
    let fieldsToStash = hydratableFields;
    // The submit pipeline strips ``query_data`` from the persisted payload
    // after uploading the FASTA to Storage, so Edit search needs to fetch
    // the original text from the api sidecar to repopulate the form
    // textarea. Skip the fetch when we already have inline FASTA or no
    // original blob is recorded.
    const hasInlineQuery =
      typeof fieldsToStash.query_data === "string" &&
      fieldsToStash.query_data.trim().length > 0;
    const hasOriginalBlob =
      Boolean(jobPayload?.query_file) ||
      Boolean(jobPayload?.query_blob_url) ||
      // External (OpenAPI) jobs project their record under `payload.external`
      // and carry no top-level query_file, but the sibling plane still stored
      // the inline FASTA at `queries/<job_id>.fa`. The backend reconstructs
      // that path, so attempt the fetch for these jobs too.
      Boolean(jobPayload?.external);
    if (!hasInlineQuery && hasOriginalBlob) {
      setLoadingQuery(true);
      try {
        const { query_text } = await blastApi.getQuery(jobId);
        fieldsToStash = { ...fieldsToStash, query_data: query_text };
      } catch (err) {
        const status =
          err && typeof err === "object" && "status" in err
            ? (err as { status?: number }).status
            : undefined;
        const code =
          err && typeof err === "object" && "body" in err
            ? ((err as { body?: { code?: string } }).body?.code ?? "")
            : "";
        if (status === 413 || code === "query_too_large_for_edit") {
          toast(
            "The original query is too large to load into the Edit form. Re-upload it manually.",
            "warning",
          );
        } else if (status === 404) {
          toast(
            "Original query is no longer available. Opening the form without it.",
            "warning",
          );
        } else {
          toast(
            `Could not load original query: ${
              err instanceof Error ? err.message : "unknown error"
            }`,
            "error",
          );
        }
      } finally {
        setLoadingQuery(false);
      }
    }
    try {
      window.sessionStorage.setItem(
        PENDING_DUPLICATE_KEY,
        JSON.stringify({
          source: { jobId, jobTitle: jobTitle ?? undefined },
          form: fieldsToStash,
        }),
      );
    } catch (err) {
      toast(
        `Could not stash search parameters: ${
          err instanceof Error ? err.message : "storage unavailable"
        }`,
        "error",
      );
      return;
    }
    toast("Search parameters copied to the New BLAST search form.", "success");
    navigate("/blast/submit");
  };

  const handleExportConfig = () => {
    if (!hydratableFields) return;
    const fullForm: FormState = {
      ...INITIAL,
      ...(hydratableFields as Partial<FormState>),
    };
    const snapshot: BlastConfigSnapshot = serializeFormToConfig({
      form: fullForm,
      source: { jobId, jobTitle: jobTitle ?? undefined },
    });
    try {
      downloadConfigJson(
        snapshot,
        buildConfigFilename({ jobId, jobTitle: jobTitle ?? undefined }),
      );
      toast("Config JSON downloaded.", "success");
    } catch (err) {
      toast(
        `Export failed: ${err instanceof Error ? err.message : "unknown error"}`,
        "error",
      );
    }
  };

  const closeDownloadMenu = () => setShowDownloadMenu(false);

  return (
    <header className="blast-job-header" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
        }}
      >
        <Link
          to="/blast/jobs"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "var(--space-2)",
            fontSize: 13,
          }}
        >
          <ArrowLeft size={14} strokeWidth={1.5} /> Recent searches
        </Link>
        <BlastHelpMenu program={program} />
      </div>

      <div
        className="blast-job-header__title-row"
        style={{
          display: "flex",
          alignItems: "center",
          gap: "var(--space-3)",
          flexWrap: "wrap",
        }}
      >
        <h1 style={{ margin: 0, flex: 1, minWidth: 0 }}>{jobTitle || jobId}</h1>
        {isRunning && createdAt && (
          <span
            style={{
              fontSize: 12,
              color: "var(--text-muted)",
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
            }}
          >
            <Clock size={12} strokeWidth={1.5} />
            <ElapsedTimer startTime={createdAt} />
          </span>
        )}
        {canCancel && (
          <button
            className="glass-button"
            onClick={onRequestCancel}
            disabled={cancelDisabled}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
              fontSize: 12,
              color: "var(--danger)",
            }}
            title="Cancel this BLAST search"
          >
            <StopCircle size={14} strokeWidth={1.5} /> Cancel
          </button>
        )}
        <button
          className="glass-button glass-button--primary"
          onClick={handleEditSearch}
          disabled={!canReuseConfig || loadingQuery}
          title={
            canReuseConfig
              ? loadingQuery
                ? "Loading original query…"
                : "Open the New BLAST search form pre-filled with these parameters."
              : "Original submit payload is not available for this search."
          }
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
            fontSize: 12,
          }}
        >
          <Edit3 size={14} strokeWidth={1.5} />{" "}
          {loadingQuery ? "Loading…" : "Edit search"}
        </button>
        <button
          className="glass-button"
          onClick={handleExportConfig}
          disabled={!canReuseConfig}
          title={
            canReuseConfig
              ? "Download this search's configuration as a JSON file."
              : "Original submit payload is not available for this search."
          }
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
            fontSize: 12,
          }}
        >
          <Download size={14} strokeWidth={1.5} /> Save settings
        </button>
        <button
          className="glass-button"
          onClick={handleCopyCitation}
          disabled={copyingCitation}
          title="Copy a reproducible Methods citation (program, version, database snapshot) to the clipboard."
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
            fontSize: 12,
          }}
        >
          {copyingCitation ? (
            <Loader2 size={14} strokeWidth={1.5} className="spin" />
          ) : (
            <Copy size={14} strokeWidth={1.5} />
          )}{" "}
          {copyingCitation ? "Copying…" : "Copy citation"}
        </button>
      </div>

      {/* The NCBI metadata grid — keep label widths consistent so the
          values line up visually no matter which fields are populated. */}
      <dl
        className="blast-job-header__meta-grid"
        style={{
          display: "grid",
          gridTemplateColumns: "min-content max-content min-content 1fr",
          rowGap: 4,
          columnGap: "var(--space-3)",
          margin: 0,
          padding: "10px 14px",
          borderRadius: 8,
          background: "var(--bg-tertiary)",
          fontSize: 13,
        }}
      >
        <Term label="Search ID" />
        <Detail>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <code className="code-val">{jobId}</code>
            <button
              type="button"
              className={`copy-btn${copiedId ? " copy-btn--copied" : ""}`}
              onClick={handleCopyId}
              title="Copy search ID"
            >
              {copiedId ? <CheckCircle2 size={12} /> : <Copy size={12} />}
            </button>
          </span>
        </Detail>
        <Term label="Submitted" />
        <Detail>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
            {submittedAt ? submittedAt.toLocaleString() : "—"}
            {hasExportTargets && (
              <DownloadAllMenu
                exportingFormat={exportingFormat}
                onExport={(format) => {
                  closeDownloadMenu();
                  onExport(format);
                }}
                open={showDownloadMenu}
                setOpen={setShowDownloadMenu}
                submittedOutfmt={submittedOutfmt}
                resultFiles={resultFiles}
              />
            )}
          </span>
        </Detail>

        <Term label="Program" />
        <Detail>{program ?? "—"}</Detail>
        <Term label="Database" />
        <Detail>
          <span style={{ wordBreak: "break-all" }}>{database ?? "—"}</span>
        </Detail>

        {timingMetrics.length > 0 && (
          <>
            <Term label="Runtime" />
            <Detail span={3}>
              <span
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  flexWrap: "wrap",
                }}
              >
                {timingMetrics.map((metric) => (
                  <span
                    key={metric.label}
                    title={metric.title}
                    style={{
                      display: "inline-flex",
                      alignItems: "baseline",
                      gap: 5,
                      padding: "2px 7px",
                      borderRadius: 6,
                      background: "color-mix(in srgb, var(--accent) 8%, transparent)",
                      border: "1px solid color-mix(in srgb, var(--accent) 16%, transparent)",
                      fontSize: 12,
                    }}
                  >
                    <span className="muted">{metric.label}</span>
                    <strong style={{ fontVariantNumeric: "tabular-nums" }}>
                      {metric.value}
                    </strong>
                  </span>
                ))}
              </span>
            </Detail>
          </>
        )}

        {databaseMetadata?.title && (
          <>
            <Term label="DB title" />
            <Detail span={3}>{databaseMetadata.title}</Detail>
          </>
        )}

        {databaseMetadata?.description && (
          <>
            <Term label="DB description" />
            <Detail span={3}>
              <span className="muted" style={{ display: "block", maxWidth: 900 }}>
                {databaseMetadata.description}
              </span>
            </Detail>
          </>
        )}

        {(databaseMetadata?.molecule_type ||
          deriveDbMolecule(program) ||
          databaseMetadata?.update_date) && (
          <>
            <Term label="DB molecule" />
            <Detail>
              {databaseMetadata?.molecule_type ?? deriveDbMolecule(program) ?? "—"}
            </Detail>
            <Term label="DB updated" />
            <Detail>{databaseMetadata?.update_date ?? "—"}</Detail>
          </>
        )}

        {(dbSequenceCount || dbLetterCount) && (
          <>
            <Term label="DB sequences" />
            <Detail>{dbSequenceCount ?? "—"}</Detail>
            <Term label="DB letters" />
            <Detail>{dbLetterCount ?? "—"}</Detail>
          </>
        )}

        {databaseMetadata?.source_version && (
          <>
            <Term label="DB snapshot" />
            <Detail span={3}>{databaseMetadata.source_version}</Detail>
          </>
        )}

        <Term label="Query ID" />
        <Detail>{queryId ?? "—"}</Detail>
        <Term label="Molecule type" />
        <Detail>{molecule ?? "—"}</Detail>

        {queryDescription && (
          <>
            <Term label="Description" />
            <Detail span={3}>
              <span className="muted" style={{ display: "block" }}>
                {queryDescription}
              </span>
            </Detail>
          </>
        )}

        {(queryLength !== null || evalue !== null || maxTargets !== null) && (
          <>
            <Term label="Query length" />
            <Detail>
              {queryLength !== null ? `${queryLength.toLocaleString()} nt` : "—"}
            </Detail>
            <Term label="E-value cutoff" />
            <Detail>
              {evalue ?? "—"}
              {maxTargets ? (
                <span className="muted" style={{ marginLeft: 10 }}>
                  · max targets {maxTargets}
                </span>
              ) : null}
            </Detail>
          </>
        )}

        {(cluster || region) && (
          <>
            <Term label="Cluster" />
            <Detail>
              <code style={{ fontSize: 11 }}>{cluster ?? "—"}</code>
            </Detail>
            <Term label="Region" />
            <Detail>{region ?? "—"}</Detail>
          </>
        )}
      </dl>
    </header>
  );
}

interface DownloadAllMenuProps {
  exportingFormat: BlastExportFormat | null;
  onExport: (format: BlastExportFormat) => void;
  open: boolean;
  setOpen: (value: boolean) => void;
  submittedOutfmt: number | null;
  resultFiles: BlastResultFile[];
}

interface DownloadAllOption {
  key: string;
  label: string;
  detail: string;
  format?: BlastExportFormat;
  disabled?: boolean;
  title?: string;
}

function DownloadAllMenu({
  exportingFormat,
  onExport,
  open,
  setOpen,
  submittedOutfmt,
  resultFiles,
}: DownloadAllMenuProps) {
  const containerRef = useRef<HTMLSpanElement | null>(null);
  const options = buildDownloadAllOptions(submittedOutfmt, resultFiles);

  // Mirror the BlastHelpMenu pattern: clicking outside or pressing Esc
  // closes the menu. Without this the menu hangs open after the user
  // clicks away, which felt broken in informal testing.
  useEffect(() => {
    if (!open) return;
    const onClick = (event: MouseEvent) => {
      const node = containerRef.current;
      if (node && !node.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, setOpen]);

  return (
    <span ref={containerRef} style={{ position: "relative" }}>
      <button
        type="button"
        className="glass-button glass-button--primary"
        onClick={() => setOpen(!open)}
        disabled={exportingFormat !== null}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
          fontSize: 12,
        }}
        title="Download all hits aggregated from every shard"
        aria-haspopup="menu"
        aria-expanded={open}
      >
        {exportingFormat !== null ? (
          <Loader2 size={12} className="spin" aria-hidden="true" />
        ) : (
          <Download size={12} strokeWidth={1.5} aria-hidden="true" />
        )}
        Download all <ChevronDown size={11} strokeWidth={1.5} aria-hidden="true" />
      </button>
      {open && (
        <div
          role="menu"
          aria-label="Download all results format"
          style={{
            position: "absolute",
            top: "100%",
            right: 0,
            marginTop: 4,
            zIndex: 10,
            minWidth: 280,
            background: "var(--bg-secondary)",
            border: "1px solid var(--glass-border)",
            borderRadius: 6,
            boxShadow: "0 4px 16px rgba(0,0,0,0.25)",
            padding: 4,
          }}
        >
          {options.map((option) => (
            <button
              key={option.key}
              type="button"
              role="menuitem"
              onClick={() => option.format && onExport(option.format)}
              disabled={exportingFormat !== null || option.disabled || !option.format}
              title={option.title}
              style={{
                display: "block",
                width: "100%",
                padding: "8px 12px",
                fontSize: 12,
                textAlign: "left",
                background: "transparent",
                border: 0,
                color: option.disabled ? "var(--text-muted)" : "var(--text-primary)",
                cursor: option.disabled ? "not-allowed" : "pointer",
                borderRadius: 4,
                opacity: option.disabled ? 0.62 : 1,
              }}
              onMouseEnter={(event) => {
                if (!option.disabled) event.currentTarget.style.background = "var(--glass-bg)";
              }}
              onMouseLeave={(event) => {
                event.currentTarget.style.background = "transparent";
              }}
            >
              <strong>{option.label}</strong>
              <span className="muted" style={{ marginLeft: 8, fontSize: 11 }}>
                {option.detail}
              </span>
            </button>
          ))}
        </div>
      )}
    </span>
  );
}

function buildDownloadAllOptions(
  submittedOutfmt: number | null,
  resultFiles: BlastResultFile[],
): DownloadAllOption[] {
  const hasFiles = resultFiles.length > 0;
  const xmlCaptured = hasFiles
    ? resultFiles.some((file) => resultFileLooksXml(file))
    : submittedOutfmt === 5;
  const textCaptured = submittedOutfmt === 0;
  const rawOnlyTitle = "Captured only when the search is submitted with this output format.";
  return [
    {
      key: "text",
      label: "Text",
      detail: textCaptured ? "Captured pairwise report" : "Submit with outfmt 0",
      format: textCaptured ? "text" : undefined,
      disabled: !textCaptured,
      title: textCaptured ? "Download the captured pairwise text report" : rawOnlyTitle,
    },
    {
      key: "xml",
      label: "XML",
      detail: xmlCaptured ? "Captured BLAST XML" : "Submit with outfmt 5",
      format: xmlCaptured ? "xml" : undefined,
      disabled: !xmlCaptured,
      title: xmlCaptured ? "Download the captured BLAST XML result" : rawOnlyTitle,
    },
    {
      key: "asn1",
      label: "ASN.1",
      detail: "Submit with outfmt 11",
      disabled: true,
      title: rawOnlyTitle,
    },
    {
      key: "json-seqalign",
      label: "JSON Seq-align",
      detail: "Derived from parsed HSPs",
      format: "json-seqalign",
      title: "Download parsed alignments as JSON",
    },
    {
      key: "hit-table-text",
      label: "Hit Table (text)",
      detail: "Tab-separated",
      format: "hit-table-text",
      title: "Download parsed hits as a tab-separated table",
    },
    {
      key: "hit-table-csv",
      label: "Hit Table (CSV)",
      detail: "Comma-separated",
      format: "hit-table-csv",
      title: "Download parsed hits as a CSV table",
    },
    {
      key: "ncbi-hit-table-text",
      label: "NCBI Descriptions (text)",
      detail: "Per-subject, NCBI columns",
      format: "ncbi-hit-table-text",
      title: "Download an NCBI Web BLAST-style Descriptions table (tab-separated)",
    },
    {
      key: "ncbi-hit-table-csv",
      label: "NCBI Descriptions (CSV)",
      detail: "Per-subject, NCBI columns",
      format: "ncbi-hit-table-csv",
      title: "Download an NCBI Web BLAST-style Descriptions table (CSV)",
    },
    {
      key: "ncbi-report-text",
      label: "NCBI Report (text)",
      detail: "With provenance header",
      format: "ncbi-report-text",
      title: "Download an NCBI-style report with an ELB provenance header",
    },
    {
      key: "multi-xml2",
      label: "Multiple-file XML2",
      detail: "Submit with outfmt 14",
      disabled: true,
      title: rawOnlyTitle,
    },
    {
      key: "single-xml2",
      label: "Single-file XML2",
      detail: "Submit with outfmt 16",
      disabled: true,
      title: rawOnlyTitle,
    },
    {
      key: "multi-json",
      label: "Multiple-file JSON",
      detail: "Submit with outfmt 13",
      disabled: true,
      title: rawOnlyTitle,
    },
    {
      key: "single-json",
      label: "Single-file JSON",
      detail: "Submit with outfmt 15",
      disabled: true,
      title: rawOnlyTitle,
    },
    {
      key: "sam",
      label: "SAM",
      detail: "Submit with outfmt 17",
      disabled: true,
      title: rawOnlyTitle,
    },
  ];
}

function resultFileLooksXml(file: BlastResultFile): boolean {
  const format = String(file.format ?? "").toLowerCase();
  const name = file.name.toLowerCase();
  return format === "blast_xml" || format === "xml" || name.endsWith(".xml") || name.endsWith(".xml.gz");
}

function Term({ label }: { label: string }) {
  return (
    <dt
      className="muted"
      style={{
        margin: 0,
        whiteSpace: "nowrap",
        fontSize: 12,
        textTransform: "uppercase",
        letterSpacing: "0.04em",
        paddingTop: 1,
      }}
    >
      {label}
    </dt>
  );
}

function Detail({ children, span }: { children: React.ReactNode; span?: number }) {
  return (
    <dd
      style={{
        margin: 0,
        gridColumn: span ? `span ${span}` : undefined,
        color: "var(--text-primary)",
      }}
    >
      {children}
    </dd>
  );
}

function pickString(
  obj: Record<string, unknown> | undefined,
  keys: string[],
): string | null {
  if (!obj) return null;
  for (const key of keys) {
    const value = obj[key];
    if (typeof value === "string" && value.trim().length > 0) return value;
    if (typeof value === "number") return String(value);
  }
  return null;
}

function pickNumber(
  obj: Record<string, unknown> | undefined,
  keys: string[],
): number | null {
  if (!obj) return null;
  for (const key of keys) {
    const value = obj[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string") {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return null;
}

function formatCount(value: number | null | undefined): string | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    return null;
  }
  return value.toLocaleString();
}

function deriveMolecule(program: string | null | undefined): string | null {
  if (!program) return null;
  switch (program.toLowerCase()) {
    case "blastn":
      return "dna";
    case "blastp":
    case "blastx":
    case "tblastn":
    case "tblastx":
      return "protein";
    default:
      return null;
  }
}

// BLAST database molecule type derived from the program convention.
// Differs from the query-side `deriveMolecule` for tblastn / tblastx
// (protein query against nucleotide database). Used as a fallback when
// the database catalogue doesn't carry an explicit molecule_type.
function deriveDbMolecule(program: string | null | undefined): string | null {
  if (!program) return null;
  switch (program.toLowerCase()) {
    case "blastn":
    case "tblastn":
    case "tblastx":
      return "nucleotide";
    case "blastp":
    case "blastx":
      return "protein";
    default:
      return null;
  }
}

function firstQueryRecordId(
  payload: Record<string, unknown> | undefined,
): string | null {
  const metadata = payload?.query_metadata;
  if (!metadata || typeof metadata !== "object") return null;
  const records = (metadata as Record<string, unknown>).records;
  if (!Array.isArray(records) || records.length === 0) return null;
  const first = records[0];
  if (!first || typeof first !== "object") return null;
  const id = (first as Record<string, unknown>).query_id;
  return typeof id === "string" && id.trim().length > 0 ? id : null;
}

function firstQueryRecordLength(
  payload: Record<string, unknown> | undefined,
): number | null {
  const metadata = payload?.query_metadata;
  if (!metadata || typeof metadata !== "object") return null;
  const records = (metadata as Record<string, unknown>).records;
  if (!Array.isArray(records) || records.length === 0) return null;
  const first = records[0];
  if (!first || typeof first !== "object") return null;
  const length = (first as Record<string, unknown>).length;
  return typeof length === "number" && Number.isFinite(length) && length > 0
    ? length
    : null;
}

function queryFileBasename(
  payload: Record<string, unknown> | undefined,
): string | null {
  const raw = pickString(payload, ["query_file", "query_blob_url"]);
  if (!raw) return null;
  const cleaned = raw.replace(/\\/g, "/");
  const tail = cleaned.includes("/") ? cleaned.split("/").pop() : cleaned;
  return tail && tail.length > 0 ? tail : null;
}

export interface TimingMetric {
  label: string;
  value: string;
  title: string;
}

export function buildTimingMetrics({
  createdAt,
  updatedAt,
  customStatus,
}: {
  createdAt: string | null;
  updatedAt?: string | null;
  customStatus?: unknown;
}): TimingMetric[] {
  const metrics: TimingMetric[] = [];
  const workflowMs = durationBetweenMs(createdAt, updatedAt ?? null);
  if (workflowMs !== null) {
    metrics.push({
      label: "Workflow",
      value: formatDurationMs(workflowMs),
      title: "Dashboard elapsed time from submit acceptance to the latest recorded update.",
    });
  }

  const steps = recordValue(recordValue(customStatus)?.steps);
  const running = recordValue(steps?.running);
  const submitting = recordValue(steps?.submitting);
  const exporting = recordValue(steps?.exporting_results);
  const k8s = recordValue(running?.k8s);

  const computeMs = numberValue(k8s?.blast_container_duration_ms);
  if (computeMs !== null) {
    metrics.push({
      label: "Compute",
      value: formatDurationMs(computeMs),
      title: "Wall-clock span of the BLAST containers across all shards.",
    });
  }

  const k8sRuntimeMs =
    numberValue(running?.duration_ms) ??
    durationBetweenMs(stringValue(k8s?.started_at), stringValue(k8s?.completed_at));
  if (k8sRuntimeMs !== null) {
    metrics.push({
      label: "K8s runtime",
      value: formatDurationMs(k8sRuntimeMs),
      title: "Kubernetes Job lifetime for the BLAST shard jobs.",
    });
  }

  const submitMs = numberValue(submitting?.duration_ms);
  if (submitMs !== null) {
    metrics.push({
      label: "Submit path",
      value: formatDurationMs(submitMs),
      title: "Dashboard and ElasticBLAST orchestration before K8s BLAST runtime starts.",
    });
  }

  const exportContainerMs = numberValue(k8s?.results_export_container_duration_ms);
  const exportWorkflowMs = numberValue(exporting?.duration_ms);
  if (exportContainerMs !== null || exportWorkflowMs !== null) {
    const value = exportContainerMs !== null
      ? formatDurationMs(exportContainerMs)
      : formatDurationMs(exportWorkflowMs ?? 0);
    metrics.push({
      label: exportContainerMs !== null ? "Export containers" : "Export path",
      value,
      title: exportContainerMs !== null && exportWorkflowMs !== null
        ? `Result export containers ran for ${value}; dashboard export/finalize path was ${formatDurationMs(exportWorkflowMs)}.`
        : exportContainerMs !== null
          ? "Wall-clock span of the result export containers."
          : "Dashboard export/finalize path after the K8s BLAST runtime completed.",
    });
  }

  return metrics;
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

function durationBetweenMs(start: string | null, end: string | null): number | null {
  if (!start || !end) return null;
  const startMs = Date.parse(start);
  const endMs = Date.parse(end);
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs) return null;
  return endMs - startMs;
}

function formatDurationMs(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const seconds = Math.round(ms / 1000);
  if (seconds <= 0) return "<1s";
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes <= 0) return `${seconds}s`;
  if (remainder === 0) return `${minutes}m`;
  return `${minutes}m ${remainder}s`;
}

// Re-exported so the parent doesn't need a second import line just to
// satisfy strict TypeScript when introspecting the hydrated shape.
export type { ExportableFormFields };
