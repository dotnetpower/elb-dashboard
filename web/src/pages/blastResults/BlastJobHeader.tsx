import { useEffect, useRef, useState } from "react";
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
import type { BlastDatabaseMetadata, BlastExportFormat } from "@/api/endpoints";
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
  isRunning: boolean;
  canCancel: boolean;
  cancelDisabled: boolean;
  onRequestCancel: () => void;
  /** Original submit payload, used by Edit search / Duplicate / Export config. */
  jobPayload?: Record<string, unknown> | undefined;
  /** NCBI-style metadata surfaced in the new 7-line header. */
  program?: string | null;
  database?: string | null;
  databaseMetadata?: BlastDatabaseMetadata | null;
  configSnapshot?: Record<string, unknown> | undefined;
  infrastructure?: Record<string, unknown> | undefined;
  /** Hooks for the "Download all results" combo. */
  exportingFormat: BlastExportFormat | null;
  onExport: (format: BlastExportFormat) => void;
  hasExportTargets: boolean;
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
  isRunning,
  canCancel,
  cancelDisabled,
  onRequestCancel,
  jobPayload,
  program,
  database,
  databaseMetadata,
  configSnapshot,
  infrastructure,
  exportingFormat,
  onExport,
  hasExportTargets,
}: BlastJobHeaderProps) {
  const navigate = useNavigate();
  const { toast } = useToast();
  const [copiedId, setCopiedId] = useState(false);
  const [showDownloadMenu, setShowDownloadMenu] = useState(false);

  const hydratableFields = jobPayload ? partialFormFromJobPayload(jobPayload) : null;
  const canReuseConfig = Boolean(hydratableFields);

  const queryId = pickString(jobPayload, ["query_id", "query_name"]);
  const queryDescription =
    pickString(jobPayload, ["query_description", "description"]) ?? jobTitle ?? null;
  const molecule = pickString(jobPayload, ["molecule_type"]) ?? deriveMolecule(program);
  const queryLength = pickNumber(jobPayload, ["query_length"]);
  const submittedAt = createdAt ? new Date(createdAt) : null;

  const cluster = pickString(infrastructure, ["cluster_name"]);
  const region = pickString(infrastructure, ["region"]);
  const evalue = pickString(configSnapshot, ["evalue"]);
  const maxTargets = pickString(configSnapshot, ["max_target_seqs"]);
  const dbSequenceCount = formatCount(databaseMetadata?.number_of_sequences);
  const dbLetterCount = formatCount(databaseMetadata?.number_of_letters);

  const handleCopyId = async () => {
    try {
      await navigator.clipboard.writeText(jobId);
      setCopiedId(true);
      setTimeout(() => setCopiedId(false), 1500);
    } catch {
      toast("Failed to copy search ID", "error");
    }
  };

  const handleEditSearch = () => {
    if (!hydratableFields) return;
    try {
      window.sessionStorage.setItem(
        PENDING_DUPLICATE_KEY,
        JSON.stringify({
          source: { jobId, jobTitle: jobTitle ?? undefined },
          form: hydratableFields,
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
    <header style={{ display: "flex", flexDirection: "column", gap: 8 }}>
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
          disabled={!canReuseConfig}
          title={
            canReuseConfig
              ? "Open the New BLAST search form pre-filled with these parameters."
              : "Original submit payload is not available for this search."
          }
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
            fontSize: 12,
          }}
        >
          <Edit3 size={14} strokeWidth={1.5} /> Edit search
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
      </div>

      {/* The NCBI metadata grid — keep label widths consistent so the
          values line up visually no matter which fields are populated. */}
      <dl
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

        {(databaseMetadata?.molecule_type || databaseMetadata?.update_date) && (
          <>
            <Term label="DB molecule" />
            <Detail>{databaseMetadata.molecule_type ?? "—"}</Detail>
            <Term label="DB updated" />
            <Detail>{databaseMetadata.update_date ?? "—"}</Detail>
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
}

function DownloadAllMenu({
  exportingFormat,
  onExport,
  open,
  setOpen,
}: DownloadAllMenuProps) {
  const containerRef = useRef<HTMLSpanElement | null>(null);

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
            minWidth: 200,
            background: "var(--bg-secondary)",
            border: "1px solid var(--glass-border)",
            borderRadius: 6,
            boxShadow: "0 4px 16px rgba(0,0,0,0.25)",
            padding: 4,
          }}
        >
          {(["csv", "tsv", "json"] as const).map((format) => (
            <button
              key={format}
              type="button"
              role="menuitem"
              onClick={() => onExport(format)}
              disabled={exportingFormat !== null}
              style={{
                display: "block",
                width: "100%",
                padding: "8px 12px",
                fontSize: 12,
                textAlign: "left",
                background: "transparent",
                border: 0,
                color: "var(--text-primary)",
                cursor: "pointer",
                borderRadius: 4,
              }}
              onMouseEnter={(event) => {
                event.currentTarget.style.background = "var(--glass-bg)";
              }}
              onMouseLeave={(event) => {
                event.currentTarget.style.background = "transparent";
              }}
            >
              <strong>{format.toUpperCase()}</strong>
              <span className="muted" style={{ marginLeft: 8, fontSize: 11 }}>
                {format === "csv"
                  ? "Comma-separated"
                  : format === "tsv"
                    ? "Tab-separated"
                    : "Newline-delimited JSON"}
              </span>
            </button>
          ))}
        </div>
      )}
    </span>
  );
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

// Re-exported so the parent doesn't need a second import line just to
// satisfy strict TypeScript when introspecting the hydrated shape.
export type { ExportableFormFields };
