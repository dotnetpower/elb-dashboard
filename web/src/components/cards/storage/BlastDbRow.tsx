import {
  AlertCircle,
  CheckCircle2,
  Circle,
  Download,
  ListOrdered,
  Loader2,
  RefreshCw,
} from "lucide-react";

import {
  type BlastDbCatalogItem,
  formatBytes,
  formatNcbiVersion,
  formatStorageDate,
} from "@/components/cards/storageDbCatalog";
import type { DownloadedDbMeta } from "@/components/cards/storage/useBlastDb";
import type { DbPreviewMeta } from "@/components/cards/storage/useDbPreviews";

interface BlastDbRowProps {
  db: BlastDbCatalogItem;
  meta: DownloadedDbMeta | undefined;
  /**
   * Live NCBI snapshot dry-run for this catalog DB. When ``available=false``
   * the DB is missing from the current S3 snapshot (FTP-only or
   * mid-publish); the row surfaces a clear hint instead of letting the
   * Download button fail with a 404 mid-copy.
   */
  preview?: DbPreviewMeta;
  isDownloaded: boolean;
  isDownloading: boolean;
  isCopying: boolean;
  inProgressInfo: { expectedFiles: number; startTime: number } | undefined;
  copyProgress: number;
  hasUpdate: boolean;
  latestVersion: string | null;
  elapsed: number;
  downloadDisabled: boolean;
  oracleBuilding: boolean;
  oracleDisabled: boolean;
  autoWarmupChecked: boolean;
  autoWarmupDisabled: boolean;
  onDownload: () => void;
  onUpdate: () => void;
  onBuildOracle: () => void;
  onConfirmLarge: () => void;
  /**
   * Abort an in-flight prepare-db (or a stuck `partial`/`init_failed` row
   * before the user retries). Optional — caller may omit when cancel is not
   * supported in a given context.
   */
  onCancel?: () => void;
  onToggleAutoWarmup: (checked: boolean) => void;
}

/**
 * Single BLAST database row inside the modal — renders the icon, title, meta,
 * progress bar, and the download/Ready chip on the right.
 *
 * Pure-presentational; all lifecycle decisions are made by the parent and
 * passed in via props.
 */
export function BlastDbRow({
  db,
  meta,
  preview,
  isDownloaded,
  isDownloading,
  isCopying,
  inProgressInfo,
  copyProgress,
  hasUpdate,
  latestVersion,
  elapsed,
  downloadDisabled,
  oracleBuilding,
  oracleDisabled,
  autoWarmupChecked,
  autoWarmupDisabled,
  onDownload,
  onUpdate,
  onBuildOracle,
  onConfirmLarge,
  onCancel,
  onToggleAutoWarmup,
}: BlastDbRowProps) {
  const triggerDownload = () => {
    if (db.category === "Large") {
      onConfirmLarge();
    } else {
      onDownload();
    }
  };
  const isUpdating = Boolean(meta?.update_in_progress);
  const copyPhase = meta?.copy_status?.phase;
  const isPartial = copyPhase === "partial" || copyPhase === "init_failed";
  const unsupported = db.unsupported;
  const isUnsupported = Boolean(unsupported);
  // Suppress the generic "Not in current NCBI snapshot" warning for DBs we
  // already know NCBI never publishes via the S3 mirror — the dedicated
  // unsupported badge carries clearer wording + the real source URL.
  const previewUnavailable =
    !isUnsupported && preview ? preview.available === false : false;
  const downloadBlocked =
    isUnsupported ||
    (downloadDisabled && !isPartial) ||
    (previewUnavailable && !isDownloaded);
  const unsupportedReasonLabel: Record<
    NonNullable<typeof unsupported>["reason"],
    string
  > = {
    "no-prebuilt": "Not provided as BLAST DB",
    "v4-only": "BLAST v4 only (incompatible)",
    "too-large": "Not bulk-distributed",
  };

  return (
    <div
      className="db-row"
      style={{
        position: "relative",
        overflow: "hidden",
        display: "grid",
        gridTemplateColumns: "20px 1fr auto",
        columnGap: 10,
        alignItems: "start",
        padding: "8px 10px",
        borderRadius: 6,
        background: isDownloaded ? "rgba(115,191,105,0.04)" : "transparent",
        border: `1px solid ${isDownloaded ? "rgba(115,191,105,0.18)" : "transparent"}`,
        marginBottom: 2,
        textAlign: "left",
        transition: "background 0.15s, border-color 0.15s",
      }}
    >
      {(isDownloading || isCopying) && (
        <div
          aria-hidden
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            right: 0,
            height: 2,
            overflow: "hidden",
            background: "rgba(122,167,255,0.12)",
            borderTopLeftRadius: 6,
            borderTopRightRadius: 6,
          }}
        >
          <div
            style={{
              width: "30%",
              height: "100%",
              background:
                "linear-gradient(90deg, transparent 0%, var(--accent) 50%, transparent 100%)",
              animation: "shimmer 1.2s linear infinite",
            }}
          />
        </div>
      )}
      <div style={{ paddingTop: 2 }}>
        {isDownloaded ? (
          <CheckCircle2 size={14} style={{ color: "var(--success)" }} />
        ) : isDownloading || isCopying ? (
          <Loader2 size={14} className="spin" style={{ color: "var(--accent)" }} />
        ) : (
          <Circle
            size={14}
            fill="currentColor"
            style={{ color: "var(--text-faint)", opacity: 0.45 }}
          />
        )}
      </div>
      <div style={{ minWidth: 0 }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            minWidth: 0,
          }}
        >
          <span
            style={{
              fontSize: 12,
              fontWeight: isDownloaded ? 600 : 500,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {db.label}
          </span>
          <span
            className={`gt ${db.type === "nucl" ? "gt-b" : "gt-p"}`}
            style={{ fontSize: 9, padding: "1px 6px", flexShrink: 0 }}
          >
            {db.type === "nucl" ? "N" : "P"}
          </span>
          <span
            style={{ fontSize: 11, color: "var(--text-muted)", flexShrink: 0 }}
          >
            {db.size}
          </span>
        </div>
        <div
          style={{
            fontSize: 11,
            color: "var(--text-muted)",
            marginTop: 2,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={db.desc}
        >
          {db.desc}
        </div>
        <div
          style={{
            fontSize: 11,
            color: "var(--text-faint)",
            marginTop: 4,
            display: "flex",
            gap: 8,
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          <code
            style={{
              fontSize: 10,
              background: "var(--bg-tertiary)",
              padding: "1px 5px",
              borderRadius: 3,
            }}
          >
            {db.value}
          </code>
          {!isDownloaded && !isDownloading && !isCopying && (
            <>
              {unsupported ? (
                <a
                  href={unsupported.sourceUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={unsupported.hint}
                  style={{
                    fontSize: 10,
                    padding: "1px 6px",
                    borderRadius: 3,
                    color: "var(--warning)",
                    background: "rgba(240,198,116,0.08)",
                    border: "1px solid rgba(240,198,116,0.24)",
                    textDecoration: "none",
                    whiteSpace: "nowrap",
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 4,
                  }}
                  onClick={(event) => event.stopPropagation()}
                >
                  <AlertCircle size={10} />{" "}
                  {unsupportedReasonLabel[unsupported.reason]} → source
                </a>
              ) : preview?.available ? (
                <>
                  <span title="Live NCBI snapshot info — fetched before download">
                    NCBI: {preview.file_count} files
                    {preview.total_bytes_estimate
                      ? ` · ~${formatBytes(preview.total_bytes_estimate)}`
                      : ""}
                  </span>
                  {preview.snapshot && (
                    <code
                      style={{
                        fontSize: 10,
                        background: "var(--bg-tertiary)",
                        padding: "1px 5px",
                        borderRadius: 3,
                      }}
                      title={`Current NCBI snapshot: ${preview.snapshot}`}
                    >
                      v:{formatNcbiVersion(preview.snapshot)}
                    </code>
                  )}
                </>
              ) : preview && preview.available === false ? (
                <span
                  style={{ color: "var(--warning)" }}
                  title={preview.message ?? "Not in current NCBI S3 snapshot"}
                >
                  Not in current NCBI snapshot
                </span>
              ) : (
                <span>
                  Est. {db.estFiles} files · {db.estMinutes}
                </span>
              )}
            </>
          )}
          {isDownloading && (
            <span style={{ color: "var(--accent)" }}>
              Initiating copy…{" "}
              <span style={{ fontFamily: "var(--font-mono)" }}>{elapsed}s</span>
            </span>
          )}
          {isCopying && inProgressInfo && (
            <span style={{ color: "var(--accent)" }}>
              Copying{" "}
              {meta?.copy_status?.success ?? meta?.file_count ?? 0} /{" "}
              {meta?.copy_status?.total_files ?? inProgressInfo.expectedFiles}{" "}
              files{" "}
              <span
                style={{
                  fontFamily: "var(--font-mono)",
                  color: "var(--text-faint)",
                }}
              >
                · {Math.floor((Date.now() - inProgressInfo.startTime) / 1000)}s
              </span>
              {db.estMinutes && (
                <span style={{ color: "var(--text-faint)" }}>
                  {" "}
                  · est. {db.estMinutes}
                </span>
              )}
            </span>
          )}
          {isPartial && meta && (
            <span
              className="db-shard-chip"
              title={
                meta.update_error ??
                "Last download did not complete. Click Get to retry."
              }
              style={{
                fontSize: 10,
                padding: "1px 6px",
                borderRadius: 3,
                color: "var(--danger)",
                background: "rgba(224,123,138,0.08)",
                border: "1px solid rgba(224,123,138,0.22)",
                fontWeight: 500,
                whiteSpace: "nowrap",
                letterSpacing: 0,
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
              }}
            >
              <AlertCircle size={10} />
              {copyPhase === "init_failed" ? "Copy init failed" : "Partial copy"}
              {meta.copy_status?.failed != null
                ? ` · ${meta.copy_status.failed} failed`
                : ""}
              {meta.copy_status?.pending
                ? ` · ${meta.copy_status.pending} pending`
                : ""}
            </span>
          )}
          {isDownloaded && meta && (
            <>
              {isUpdating && (
                <span
                  className="db-shard-chip"
                  title={
                    meta.updating_to_source_version
                      ? `Updating to ${formatNcbiVersion(meta.updating_to_source_version)}`
                      : "Updating DB generation"
                  }
                  style={{
                    fontSize: 10,
                    padding: "1px 6px",
                    borderRadius: 3,
                    color: "var(--accent)",
                    background: "rgba(110,159,255,0.10)",
                    border: "1px solid rgba(110,159,255,0.28)",
                    fontWeight: 500,
                    whiteSpace: "nowrap",
                    letterSpacing: 0,
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 4,
                  }}
                >
                  <Loader2 size={10} className="spin" /> Updating
                </span>
              )}
              {meta.update_error && !isUpdating && (
                <span
                  className="db-shard-chip"
                  title={meta.update_error}
                  style={{
                    fontSize: 10,
                    padding: "1px 6px",
                    borderRadius: 3,
                    color: "var(--warning)",
                    background: "rgba(240,198,116,0.08)",
                    border: "1px solid rgba(240,198,116,0.24)",
                    fontWeight: 500,
                    whiteSpace: "nowrap",
                    letterSpacing: 0,
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 4,
                  }}
                >
                  <AlertCircle size={10} /> Update failed
                </span>
              )}
              {meta.total_bytes ? (
                <span style={{ color: "var(--success)" }}>
                  {formatBytes(meta.total_bytes)}
                </span>
              ) : null}
              {meta.file_count ? <span>{meta.file_count} files</span> : null}
              {meta.last_modified ? (
                <span>{formatStorageDate(meta.last_modified)}</span>
              ) : null}
              {meta.source_version && (
                <code
                  style={{
                    fontSize: 10,
                    background: "var(--bg-tertiary)",
                    padding: "1px 5px",
                    borderRadius: 3,
                  }}
                >
                  v:{formatNcbiVersion(meta.source_version)}
                </code>
              )}
              {meta.sharded && (meta.shard_sets?.length ?? 0) > 0 && (
                <span
                  className="db-shard-chip"
                  title={
                    meta.shards_stale
                      ? `Shard layouts were built for ${formatNcbiVersion(meta.shard_source_version ?? "")}; rebuild before relying on them for ${formatNcbiVersion(meta.source_version ?? "")}.`
                      : `Pre-built shard layouts: N = ${meta.shard_sets!.join(", ")}. Auto-selected per submit based on cluster size & RAM.`
                  }
                  style={{
                    fontSize: 10,
                    padding: "1px 6px",
                    borderRadius: 3,
                    color: meta.shards_stale ? "var(--warning)" : "var(--accent)",
                    background: meta.shards_stale
                      ? "rgba(240,198,116,0.08)"
                      : "rgba(110,159,255,0.10)",
                    border: meta.shards_stale
                      ? "1px solid rgba(240,198,116,0.24)"
                      : "1px solid rgba(110,159,255,0.28)",
                    fontWeight: 500,
                    whiteSpace: "nowrap",
                    letterSpacing: 0,
                  }}
                >
                  {meta.shards_stale ? "Shards stale" : "Sharded"} ·{" "}
                  {meta.shard_sets!.length} layouts
                </span>
              )}
              {meta.db_order_oracle && (
                <span
                  className="db-shard-chip"
                  title={`DB order oracle: ${meta.db_order_oracle.ready_parts ?? 0}/${meta.db_order_oracle.expected_parts ?? 0} parts`}
                  style={{
                    fontSize: 10,
                    padding: "1px 6px",
                    borderRadius: 3,
                    color:
                      meta.db_order_oracle.status === "ready"
                        ? "var(--success)"
                        : meta.db_order_oracle.status === "stale"
                          ? "var(--warning)"
                        : "var(--accent)",
                    background:
                      meta.db_order_oracle.status === "stale"
                        ? "rgba(240,198,116,0.08)"
                        : "rgba(106,214,163,0.08)",
                    border:
                      meta.db_order_oracle.status === "stale"
                        ? "1px solid rgba(240,198,116,0.24)"
                        : "1px solid rgba(106,214,163,0.22)",
                    fontWeight: 500,
                    whiteSpace: "nowrap",
                    letterSpacing: 0,
                  }}
                >
                  {meta.db_order_oracle.status === "stale"
                    ? "Order stale"
                    : `Order · ${meta.db_order_oracle.status}`}
                </span>
              )}
            </>
          )}
          <label
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
              color: autoWarmupChecked ? "var(--success)" : "var(--text-muted)",
              cursor: autoWarmupDisabled ? "not-allowed" : "pointer",
              opacity: autoWarmupDisabled ? 0.55 : 1,
            }}
            title={
              autoWarmupDisabled
                ? !isDownloaded
                  ? "Download this database before enabling automatic warmup"
                  : isUpdating
                    ? "Automatic warmup waits until this update finishes"
                    : hasUpdate
                      ? "Update this database before enabling automatic warmup"
                      : "Automatic warmup is unavailable"
                : "Warm this database automatically when an AKS workload cluster is running"
            }
          >
            <input
              type="checkbox"
              checked={autoWarmupChecked}
              disabled={autoWarmupDisabled}
              onChange={(event) => onToggleAutoWarmup(event.target.checked)}
              style={{ accentColor: "var(--success)", margin: 0 }}
            />
            Auto warm
          </label>
        </div>
        {isCopying && inProgressInfo && (
          <div
            style={{
              marginTop: 5,
              height: 3,
              background: "var(--bg-tertiary)",
              borderRadius: 2,
              overflow: "hidden",
            }}
          >
            <div
              style={{
                width: `${copyProgress}%`,
                height: "100%",
                background: "var(--accent)",
                transition: "width 0.5s ease",
              }}
            />
          </div>
        )}
      </div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "flex-end",
          minWidth: 84,
          paddingTop: 1,
        }}
      >
        {isUpdating ? (
          <span
            className="gt gt-b"
            style={{ fontSize: 10, display: "inline-flex", alignItems: "center", gap: 4 }}
          >
            <Loader2 size={10} className="spin" /> Updating
          </span>
        ) : hasUpdate ? (
          <button
            className="glass-button"
            style={{
              fontSize: 11,
              padding: "3px 10px",
              color: "var(--warning)",
              borderColor: "rgba(240,198,116,0.3)",
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
            }}
            onClick={(e) => {
              e.stopPropagation();
              onUpdate();
            }}
            disabled={downloadDisabled}
            title={`Update from ${formatNcbiVersion(meta!.source_version!)} to ${formatNcbiVersion(latestVersion!)}`}
          >
            <RefreshCw size={11} /> Update
          </button>
        ) : isDownloaded ? (
          <div style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            <button
              className="glass-button"
              style={{
                fontSize: 11,
                padding: "3px 6px",
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
              }}
              onClick={(e) => {
                e.stopPropagation();
                onBuildOracle();
              }}
              disabled={oracleDisabled}
              title="Build DB order oracle from warmed AKS shards"
            >
              {oracleBuilding ? (
                <Loader2 size={11} className="spin" />
              ) : (
                <ListOrdered size={11} />
              )}
            </button>
            <span className="gt gt-g" style={{ fontSize: 10 }}>
              Ready
            </span>
          </div>
        ) : isDownloading ? (
          <span
            className="gt gt-b"
            style={{ fontSize: 10, fontFamily: "var(--font-mono)" }}
          >
            {elapsed}s
          </span>
        ) : isCopying ? (
          <div style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            <span className="gt gt-b" style={{ fontSize: 10 }}>
              {copyProgress}%
            </span>
            {onCancel && (
              <button
                className="glass-button"
                style={{
                  fontSize: 10,
                  padding: "2px 6px",
                  color: "var(--danger)",
                  borderColor: "rgba(224,123,138,0.3)",
                }}
                onClick={(e) => {
                  e.stopPropagation();
                  onCancel();
                }}
                title="Cancel in-flight download"
              >
                Cancel
              </button>
            )}
          </div>
        ) : (
          <button
            className="glass-button glass-button--primary"
            style={{
              fontSize: 11,
              padding: "3px 10px",
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
            }}
            onClick={(e) => {
              e.stopPropagation();
              triggerDownload();
            }}
            disabled={downloadBlocked}
            title={
              isUnsupported && unsupported
                ? unsupported.hint
                : previewUnavailable
                  ? (preview?.message ??
                    "Not in current NCBI S3 snapshot. Retry once the snapshot rotates.")
                  : downloadDisabled
                    ? "Another download is in progress"
                    : `Download ${db.value}`
            }
          >
            <Download size={11} /> {isPartial ? "Retry" : "Get"}
          </button>
        )}
      </div>
    </div>
  );
}
