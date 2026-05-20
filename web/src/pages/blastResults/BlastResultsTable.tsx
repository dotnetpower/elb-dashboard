import { Link } from "react-router-dom";
import {
  AlertTriangle,
  Download,
  FileJson,
  FileSearch,
  Loader2,
  RefreshCw,
} from "lucide-react";

import { formatBytes } from "@/components/BlastFilePreview";
import type { BlastResultFile } from "@/api/endpoints";
import { isFeatureEnabled } from "@/config/runtime";
import { classifyBlastResultFile } from "@/pages/blastResultsModel";

interface BlastResultsTableProps {
  files: BlastResultFile[];
  resultFiles: BlastResultFile[];
  supportFiles: BlastResultFile[];
  debugFiles: BlastResultFile[];
  hasOnlyDebugFiles: boolean;
  downloadingFile: string | null;
  onDownload: (file: BlastResultFile) => void;
}

/**
 * The table of result files at the bottom of the page. Tags each row as
 * RESULT / LOG / INFO based on the extension; below the table, surfaces a
 * "diagnostic-only" warning and an expandable section for the debug files
 * when both result and debug files are present.
 */
export function BlastResultsTable({
  files,
  resultFiles,
  supportFiles,
  debugFiles,
  hasOnlyDebugFiles,
  downloadingFile,
  onDownload,
}: BlastResultsTableProps) {
  const primaryFiles = resultFiles.length > 0 ? resultFiles : files;
  const showPrimaryAsDiagnostics = resultFiles.length === 0 && hasOnlyDebugFiles;
  const primaryTitle = showPrimaryAsDiagnostics
    ? "Diagnostic files"
    : resultFiles.length > 0
      ? "Primary outputs"
      : "Supporting artifacts";
  const primaryDescription = showPrimaryAsDiagnostics
    ? "Logs and cluster status captured for this job."
    : resultFiles.length > 0
      ? "BLAST output files ready for download."
      : "No primary BLAST output was found; these artifacts may help explain the run.";

  return (
    <div style={{ marginTop: "var(--space-3)" }}>
      <ResultSectionHeader
        title={primaryTitle}
        description={primaryDescription}
        count={primaryFiles.length}
      />
      <ResultsFileTable
        files={primaryFiles}
        downloadingFile={downloadingFile}
        onDownload={onDownload}
      />
      {hasOnlyDebugFiles && (
        <div
          style={{
            marginTop: 12,
            padding: "10px 14px",
            borderRadius: 8,
            background: "rgba(240,198,116,0.08)",
            fontSize: 12,
            color: "var(--text-muted)",
          }}
        >
          <AlertTriangle
            size={13}
            style={{
              verticalAlign: "middle",
              marginRight: 6,
              color: "var(--warning)",
            }}
          />
          BLAST produced no `.out` result files. The files above are diagnostic logs
          from the cluster — most commonly this means the search returned no hits
          for the query/database combination.
        </div>
      )}
      {supportFiles.length > 0 && resultFiles.length > 0 && (
        <ArtifactDetails
          icon="support"
          title="Reports and manifests"
          files={supportFiles}
          downloadingFile={downloadingFile}
          onDownload={onDownload}
        />
      )}
      {debugFiles.length > 0 && !hasOnlyDebugFiles && (
        <ArtifactDetails
          icon="diagnostic"
          title="Diagnostic logs"
          files={debugFiles}
          downloadingFile={downloadingFile}
          onDownload={onDownload}
        />
      )}
    </div>
  );
}

function ResultSectionHeader({
  title,
  description,
  count,
}: {
  title: string;
  description: string;
  count: number;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "baseline",
        justifyContent: "space-between",
        gap: 12,
        marginBottom: 8,
      }}
    >
      <div>
        <div style={{ color: "var(--text-primary)", fontSize: 13, fontWeight: 600 }}>
          {title}
        </div>
        <div style={{ color: "var(--text-muted)", fontSize: 11, marginTop: 2 }}>
          {description}
        </div>
      </div>
      <span className="muted" style={{ fontSize: 11, whiteSpace: "nowrap" }}>
        {count} file{count === 1 ? "" : "s"}
      </span>
    </div>
  );
}

function ResultsFileTable({
  files,
  downloadingFile,
  onDownload,
}: {
  files: BlastResultFile[];
  downloadingFile: string | null;
  onDownload: (file: BlastResultFile) => void;
}) {
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
      <thead>
        <tr style={{ borderBottom: "1px solid var(--glass-border)" }}>
          <ResultsHeaderCell label="File" align="left" />
          <ResultsHeaderCell label="Size" align="right" />
          <ResultsHeaderCell label="Modified" align="right" />
          <th style={{ width: 60 }} />
        </tr>
      </thead>
      <tbody>
        {files.map((file) => (
          <BlastResultRow
            key={file.name}
            file={file}
            isDownloading={downloadingFile === file.name}
            onDownload={() => onDownload(file)}
          />
        ))}
      </tbody>
    </table>
  );
}

function ArtifactDetails({
  icon,
  title,
  files,
  downloadingFile,
  onDownload,
}: {
  icon: "support" | "diagnostic";
  title: string;
  files: BlastResultFile[];
  downloadingFile: string | null;
  onDownload: (file: BlastResultFile) => void;
}) {
  const Icon = icon === "support" ? FileJson : FileSearch;
  return (
    <details style={{ marginTop: 14, fontSize: 12 }}>
      <summary
        style={{
          cursor: "pointer",
          color: "var(--text-muted)",
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        <Icon size={13} strokeWidth={1.5} />
        {title} ({files.length})
      </summary>
      <div style={{ marginTop: 8 }}>
        <ResultsFileTable
          files={files}
          downloadingFile={downloadingFile}
          onDownload={onDownload}
        />
      </div>
    </details>
  );
}

function ResultsHeaderCell({ label, align }: { label: string; align: "left" | "right" }) {
  return (
    <th
      style={{
        textAlign: align,
        padding: "8px 12px",
        color: "var(--text-muted)",
        fontWeight: 500,
        fontSize: 11,
        textTransform: "uppercase",
        letterSpacing: "0.06em",
      }}
    >
      {label}
    </th>
  );
}

function BlastResultRow({
  file,
  isDownloading,
  onDownload,
}: {
  file: BlastResultFile;
  isDownloading: boolean;
  onDownload: () => void;
}) {
  const fname = file.name.split("/").pop() || file.name;
  const directory = file.name.includes("/")
    ? file.name.split("/").slice(0, -1).join("/")
    : "";
  const kind = classifyBlastResultFile(file);
  const typeColor =
    kind === "result"
      ? "var(--success)"
      : kind === "support"
        ? "var(--accent)"
        : "var(--warning)";
  const typeLabel = kind === "result" ? "RESULT" : kind === "support" ? "REPORT" : "LOG";

  return (
    <tr style={{ borderBottom: "1px solid var(--glass-border)" }}>
      <td
        style={{
          padding: "8px 12px",
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <span
          style={{
            fontSize: 9,
            padding: "1px 5px",
            borderRadius: 3,
            background: `color-mix(in srgb, ${typeColor} 15%, transparent)`,
            color: typeColor,
            fontWeight: 600,
            letterSpacing: "0.04em",
            flexShrink: 0,
          }}
        >
          {typeLabel}
        </span>
        <span style={{ minWidth: 0 }}>
          <code style={{ fontSize: 12 }}>{fname}</code>
          {directory && (
            <span
              className="muted"
              style={{ display: "block", fontSize: 10, marginTop: 2 }}
            >
              {directory}/
            </span>
          )}
        </span>
      </td>
      <td style={{ padding: "8px 12px", textAlign: "right" }} className="muted">
        {file.size != null ? formatBytes(file.size) : "—"}
      </td>
      <td style={{ padding: "8px 12px", textAlign: "right" }} className="muted">
        {file.last_modified ? new Date(file.last_modified).toLocaleString() : "—"}
      </td>
      <td style={{ padding: "8px 12px", textAlign: "right" }}>
        <button
          className="glass-button"
          onClick={onDownload}
          disabled={isDownloading}
          title="Download"
        >
          {isDownloading ? (
            <Loader2 size={14} className="spin" />
          ) : (
            <Download size={14} strokeWidth={1.5} />
          )}
        </button>
      </td>
    </tr>
  );
}

interface NoResultFilesPanelProps {
  jobId: string;
  storageAccount: string;
  terminalSidecarHealthy: boolean;
  hasRunningCluster: boolean;
  hasAnyCluster: boolean;
  onRetry: () => void;
}

/**
 * Empty-state panel rendered when phase=completed but no files were listed.
 * Most commonly means BLAST returned no hits — provide quick recovery
 * affordances (retry, terminal, re-submit with same parameters).
 */
export function NoResultFilesPanel({
  jobId,
  storageAccount,
  terminalSidecarHealthy,
  hasRunningCluster,
  hasAnyCluster,
  onRetry,
}: NoResultFilesPanelProps) {
  const terminalEnabled = isFeatureEnabled("terminal");

  return (
    <div
      style={{
        padding: "16px",
        borderRadius: 10,
        background: "var(--bg-tertiary)",
      }}
    >
      <div style={{ fontSize: 13, color: "var(--text-primary)", marginBottom: 10 }}>
        <strong>No significant similarity found.</strong>
      </div>
      <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.6 }}>
        BLAST returned no hits for this query / database combination. Try lowering the
        identity threshold, raising the maximum E-value, or selecting a broader
        database. The output files (if any) are listed below as{" "}
        <code style={{ fontSize: 11 }}>results/{jobId}/</code>.
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 10 }}>
        <button className="glass-button" onClick={onRetry} style={{ fontSize: 12 }}>
          <RefreshCw size={13} /> Try Again
        </button>
        {terminalEnabled &&
          (terminalSidecarHealthy ? (
            <Link
              to="/terminal"
              className="glass-button"
              style={{ textDecoration: "none", fontSize: 12 }}
            >
              <Download size={13} /> Check Terminal
            </Link>
          ) : (
            <button
              type="button"
              className="glass-button"
              disabled
              title="Terminal sidecar is not available in this environment"
              style={{ fontSize: 12, cursor: "not-allowed" }}
            >
              <Download size={13} /> Check Terminal
            </button>
          ))}
        {hasRunningCluster ? (
          <Link
            to={`/blast/submit?resubmit=${encodeURIComponent(jobId)}`}
            className="glass-button glass-button--primary"
            style={{ textDecoration: "none", fontSize: 12 }}
          >
            <Download size={13} /> Re-submit with Same Parameters
          </Link>
        ) : (
          <button
            type="button"
            className="glass-button"
            disabled
            title={
              hasAnyCluster
                ? "AKS cluster is not running — start it on the Dashboard"
                : "Provision an AKS cluster on the Dashboard first"
            }
            style={{ fontSize: 12, cursor: "not-allowed" }}
          >
            <Download size={13} /> Re-submit with Same Parameters
          </button>
        )}
      </div>
      <div
        style={{
          marginTop: 14,
          paddingTop: 12,
          borderTop: "1px solid var(--border-weak)",
          display: "grid",
          gridTemplateColumns: "80px 1fr",
          gap: "3px 10px",
          fontSize: 11,
          color: "var(--text-faint)",
        }}
      >
        <span>Account</span>
        <code style={{ fontSize: 11 }}>{storageAccount}</code>
        <span>Container</span>
        <code style={{ fontSize: 11 }}>results</code>
        <span>Prefix</span>
        <code style={{ fontSize: 11 }}>{jobId}/</code>
      </div>
    </div>
  );
}
