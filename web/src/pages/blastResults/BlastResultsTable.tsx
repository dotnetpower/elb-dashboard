import { Link } from "react-router-dom";
import {
  AlertTriangle,
  Download,
  Loader2,
  RefreshCw,
} from "lucide-react";

import { formatBytes } from "@/components/BlastFilePreview";
import type { BlastResultFile } from "@/api/endpoints";

interface BlastResultsTableProps {
  files: BlastResultFile[];
  resultFiles: BlastResultFile[];
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
  debugFiles,
  hasOnlyDebugFiles,
  downloadingFile,
  onDownload,
}: BlastResultsTableProps) {
  return (
    <div style={{ marginTop: "var(--space-3)" }}>
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
          {files.map((f) => (
            <BlastResultRow
              key={f.name}
              file={f}
              isDownloading={downloadingFile === f.name}
              onDownload={() => onDownload(f)}
            />
          ))}
        </tbody>
      </table>
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
          No BLAST result files (.out) were produced. The files above are diagnostic
          logs from the cluster. This typically means the search returned no hits for
          the query/database combination.
        </div>
      )}
      {debugFiles.length > 0 && resultFiles.length > 0 && (
        <details style={{ marginTop: 14, fontSize: 12 }}>
          <summary style={{ cursor: "pointer", color: "var(--text-muted)" }}>
            {debugFiles.length} diagnostic file{debugFiles.length > 1 ? "s" : ""} (logs,
            status)
          </summary>
          <div style={{ marginTop: 8, display: "flex", flexWrap: "wrap", gap: 8 }}>
            {debugFiles.map((f) => {
              const fname = f.name.split("/").pop() || f.name;
              return (
                <button
                  key={f.name}
                  className="glass-button"
                  style={{ fontSize: 11, padding: "4px 10px" }}
                  onClick={() => onDownload(f)}
                >
                  <Download size={11} /> {fname}
                </button>
              );
            })}
          </div>
        </details>
      )}
    </div>
  );
}

function ResultsHeaderCell({
  label,
  align,
}: {
  label: string;
  align: "left" | "right";
}) {
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
  const ext = fname.split(".").pop()?.toLowerCase() || "";
  const isResult = ext === "out" || ext === "gz" || ext === "asn";
  const isLog = ext === "log";
  const typeColor = isResult
    ? "var(--success)"
    : isLog
      ? "var(--warning)"
      : "var(--text-faint)";
  const typeLabel = isResult ? "RESULT" : isLog ? "LOG" : "INFO";

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
        <code style={{ fontSize: 12 }}>{fname}</code>
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
  return (
    <div
      style={{
        padding: "16px",
        borderRadius: 10,
        background: "var(--bg-tertiary)",
      }}
    >
      <div style={{ fontSize: 13, color: "var(--text-primary)", marginBottom: 10 }}>
        No BLAST result files (.out) found in{" "}
        <code style={{ fontSize: 12 }}>results/{jobId}/</code>.
      </div>
      <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.6 }}>
        This typically means the BLAST search returned no hits for the given query
        and database combination.
      </div>
      <div
        style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 10 }}
      >
        <button
          className="glass-button"
          onClick={onRetry}
          style={{ fontSize: 12 }}
        >
          <RefreshCw size={13} /> Try Again
        </button>
        {terminalSidecarHealthy ? (
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
        )}
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
