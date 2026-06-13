import {
  AlertCircle,
  CheckCircle2,
  Circle,
  Download,
  ExternalLink,
  ListOrdered,
  Loader2,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import {
  computeWindowedBytesPerSec,
  computeWindowedSpeed,
  formatEta,
  formatEtaFromBytes,
  recordSpeedSample,
  type SpeedSample,
} from "@/components/cards/storage/blastDbProgress";
import {
  type BlastDbCatalogItem,
  formatBytes,
  formatNcbiVersion,
  formatStorageDate,
  ncbiBlastDbFtpUrl,
} from "@/components/cards/storageDbCatalog";
import type { DownloadedDbMeta } from "@/components/cards/storage/useBlastDb";
import type { DbPreviewMeta } from "@/components/cards/storage/useDbPreviews";
import { useMonotonicPercent } from "@/hooks/useMonotonicPercent";

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
  hasUpdate: boolean;
  latestVersion: string | null;
  elapsed: number;
  downloadDisabled: boolean;
  oracleBuilding: boolean;
  oracleDisabled: boolean;
  /**
   * Tooltip explaining why the Build Oracle button is disabled when the
   * reason is not RBAC (e.g. the AKS cluster is stopped). Falls back to the
   * generic build hint when undefined.
   */
  oracleDisabledReason?: string;
  autoWarmupChecked: boolean;
  autoWarmupDisabled: boolean;
  /**
   * When true the caller lacks the Azure RBAC role needed to mutate this
   * database (Reader-only at the requested scope). Every write action
   * (Get / Retry / Update / Build Oracle / Cancel / Auto warm) is rendered
   * disabled with ``writeDisabledReason`` as the tooltip. Defaults to false.
   */
  writeDisabled?: boolean;
  writeDisabledReason?: string;
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
  /**
   * True while an abort request for this row is in flight. Renders the Cancel
   * button as a disabled "Cancelling…" spinner so the action gives immediate
   * feedback instead of looking unchanged until the network call resolves.
   */
  isCancelling?: boolean;
  /**
   * Permanently delete a staged database (all shard blobs + metadata).
   * Optional — caller may omit when delete is not supported in a given
   * context. Shown for a Ready row and for a `partial`/`cancelled` leftover.
   */
  onDelete?: () => void;
  /**
   * True while a delete request for this row is in flight. Renders the Delete
   * button as a disabled spinner.
   */
  isDeleting?: boolean;
  onToggleAutoWarmup: (checked: boolean) => void;
}

/**
 * Single BLAST database row inside the modal — renders the icon, title, meta,
 * progress bar, and the download/Ready chip on the right.
 *
 * Pure-presentational; all lifecycle decisions are made by the parent and
 * passed in via props.
 */

/**
 * Loading placeholder for a single catalog row. Rendered while the database
 * list is still being fetched, so the modal never shows actionable
 * Download/Update/Delete buttons before the real downloaded-state is known
 * (those buttons would all look "active" because the empty list makes every
 * row appear not-yet-downloaded). Mirrors the real row's
 * ``20px 1fr auto`` grid so the layout does not jump when data arrives.
 */
export function BlastDbRowSkeleton() {
  return (
    <div
      aria-hidden
      style={{
        display: "grid",
        gridTemplateColumns: "20px 1fr auto",
        columnGap: 10,
        alignItems: "center",
        padding: "8px 10px",
        borderRadius: 6,
        border: "1px solid transparent",
        marginBottom: 2,
      }}
    >
      <span
        className="skeleton"
        style={{ width: 14, height: 14, borderRadius: "50%" }}
      />
      <span style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <span className="skeleton" style={{ width: "42%", height: 12 }} />
        <span className="skeleton" style={{ width: "68%", height: 9 }} />
      </span>
      <span
        className="skeleton"
        style={{ width: 84, height: 24, borderRadius: 6 }}
      />
    </div>
  );
}

export function BlastDbRow({
  db,
  meta,
  preview,
  isDownloaded,
  isDownloading,
  isCopying,
  inProgressInfo,
  hasUpdate,
  latestVersion,
  elapsed,
  downloadDisabled,
  oracleBuilding,
  oracleDisabled,
  oracleDisabledReason,
  autoWarmupChecked,
  autoWarmupDisabled,
  writeDisabled = false,
  writeDisabledReason,
  onDownload,
  onUpdate,
  onBuildOracle,
  onConfirmLarge,
  onCancel,
  isCancelling = false,
  onDelete,
  isDeleting = false,
  onToggleAutoWarmup,
}: BlastDbRowProps) {
  const triggerDownload = () => {
    if (db.category === "Large") {
      onConfirmLarge();
    } else {
      onDownload();
    }
  };
  // Live copy progress. The number is clamped to be monotonic non-decreasing
  // within a single copy session so it never flickers between
  // `copy_status.success` and the live blob-listing `file_count` (which
  // fluctuates while azcopy/server-side copies are mid-flight). We only trust
  // `copy_status.success` here and never fall back to `file_count` during an
  // active copy.
  //
  // A copy can be live for two reasons: (1) this browser session started it,
  // so `isCopying`/`inProgressInfo` are populated by useBlastDb; or (2) the
  // server reports an in-flight copy via the metadata — `copy_status.phase` of
  // `copying`/`queued`, or an `update_in_progress` generation swap. Case (2)
  // happens after a page reload, or when the copy/update was launched from
  // another tab/session: the local `inProgress` map is empty but the metadata
  // still carries honest progress. Previously the progress text + bar were
  // gated on `inProgressInfo` alone, so a reloaded update showed only the
  // "Updating" badge with no numbers.
  const isUpdating = Boolean(meta?.update_in_progress);
  const copyPhase = meta?.copy_status?.phase;
  const isPartial = copyPhase === "partial" || copyPhase === "init_failed";
  const serverCopyActive =
    copyPhase === "copying" || copyPhase === "queued" || isUpdating;
  const copyActive = (isCopying && Boolean(inProgressInfo)) || serverCopyActive;

  const cs = meta?.copy_status;
  // The server-side copy path reports a per-file `success`; the AKS-fanout
  // path reports pod-level counts (`succeeded_pods`/`shard_count`) instead.
  // Prefer per-file progress and fall back to shard progress so an AKS update
  // still surfaces a moving bar instead of a bare "Updating" badge.
  const hasPerFile = typeof cs?.success === "number";
  const hasShard =
    typeof cs?.succeeded_pods === "number" && typeof cs?.shard_count === "number";
  const maxCopiedRef = useRef(0);
  if (!copyActive) {
    maxCopiedRef.current = 0;
  }
  const rawCopied = copyActive ? (cs?.success ?? 0) : 0;
  if (rawCopied > maxCopiedRef.current) {
    maxCopiedRef.current = rawCopied;
  }
  const copiedFiles = maxCopiedRef.current;
  const perFileTotal = cs?.total_files ?? inProgressInfo?.expectedFiles ?? 0;
  // Unit-agnostic progress: per-file (server-side) or shard (AKS).
  const progressDone = hasPerFile
    ? copiedFiles
    : hasShard
      ? (cs?.succeeded_pods ?? 0)
      : 0;
  const progressTotal = hasPerFile
    ? perFileTotal
    : hasShard
      ? (cs?.shard_count ?? 0)
      : perFileTotal;
  const progressUnit = hasPerFile || !hasShard ? "files" : "shards";
  const rawCopyPct =
    progressTotal > 0
      ? Math.min(100, Math.round((progressDone / progressTotal) * 100))
      : 0;
  // Clamp the bar so it never rewinds within one copy session. A transient
  // blob-listing failure drops `copy_status.success`, which flips the source
  // from the fine-grained per-file percent (e.g. 80%) to the coarse shard
  // percent (`succeeded_pods`/`shard_count`, often still 0/10 = 0%) for one
  // poll — without this the bar visibly collapses then recovers. The reset key
  // is the copy session start so a brand-new copy/update begins from 0.
  const copyPct = useMonotonicPercent(rawCopyPct, {
    resetKey: `${inProgressInfo?.startTime ?? meta?.update_started_at ?? ""}`,
    active: copyActive,
  });
  // Elapsed seconds: prefer the local session start (most accurate), then the
  // server-recorded update start so the ETA survives a page reload.
  const copyStartMs = inProgressInfo
    ? inProgressInfo.startTime
    : meta?.update_started_at
      ? Date.parse(meta.update_started_at)
      : NaN;
  const copyElapsedSeconds = Number.isFinite(copyStartMs)
    ? Math.max(0, Math.floor((Date.now() - copyStartMs) / 1000))
    : 0;
  // Live *instantaneous* download speed from recent `bytes_done` samples.
  // Only the AKS-fanout path reports `bytes_done`; the server-side
  // blob-to-blob copy does not (no real network download on the worker side).
  // Averaging over the whole copy understates the rate because the AKS pods
  // spend the first ~30-60 s scheduling / pulling images / scanning NCBI S3
  // with zero bytes landed, so we sample bytes over a trailing window (in an
  // effect — sampling is a side effect, never done during render) and project
  // an instantaneous rate, hiding the figure when nothing advances recently.
  const speedSamplesRef = useRef<SpeedSample[]>([]);
  const [speedLabel, setSpeedLabel] = useState("");
  // Byte-based ETA label, derived from the same trailing-window rate (AKS
  // path only). Held in state because it is computed in the sampling effect.
  const [byteEtaLabel, setByteEtaLabel] = useState<string | null>(null);
  const bytesDone =
    copyActive && typeof cs?.bytes_done === "number" ? cs.bytes_done : null;
  const bytesTotal =
    copyActive && typeof cs?.bytes_total === "number" ? cs.bytes_total : null;
  useEffect(() => {
    if (!copyActive) {
      speedSamplesRef.current = [];
      setSpeedLabel("");
      setByteEtaLabel(null);
      return;
    }
    if (bytesDone === null) return;
    const now = Date.now();
    speedSamplesRef.current = recordSpeedSample(
      speedSamplesRef.current,
      bytesDone,
      now,
    );
    setSpeedLabel(computeWindowedSpeed(speedSamplesRef.current, now));
    // Byte-based ETA: bytes still to land over the trailing-window throughput.
    // The windowed rate reflects only recent movement, so it is immune to the
    // startup inflation a re-run causes — a re-run finds thousands of small
    // blobs already staged, which spikes the file-count rate to a near-instant
    // bogus "~12s left" even though the remaining multi-GB `.nsq` volumes take
    // many minutes. The byte projection stays honest there.
    const rate = computeWindowedBytesPerSec(speedSamplesRef.current, now);
    if (rate !== null && bytesTotal !== null) {
      setByteEtaLabel(formatEtaFromBytes(bytesTotal - bytesDone, rate) || null);
    } else {
      setByteEtaLabel(null);
    }
    // `elapsed` advances ~1 Hz via the parent tick, so a stalled copy still
    // re-runs this effect and `computeWindowedSpeed` clears the stale rate.
  }, [copyActive, bytesDone, bytesTotal, elapsed]);
  // Dynamic remaining-time estimate. Prefer the byte-based projection (AKS
  // path, `bytes_total` present): file-count extrapolation mis-estimates badly
  // when the remaining files are the largest volumes or a re-run pre-stages
  // many blobs instantly. Fall back to the file-count `formatEta` only for the
  // server-side path, which reports no byte totals. When the AKS byte rate is
  // momentarily unavailable (between PipeBlob commits) `byteEtaLabel` is null
  // and the "transferring large volumes…" note covers the gap instead of a
  // misleading count-based figure.
  const etaLabel =
    bytesTotal !== null
      ? byteEtaLabel
      : hasPerFile && copyElapsedSeconds > 0
        ? formatEta(copyElapsedSeconds, copiedFiles, perFileTotal)
        : null;
  // "Looks frozen but isn't" guard. The AKS pods stream each NCBI file to a
  // block blob with azcopy `--from-to=PipeBlob`, which COMMITS the blob only
  // when the whole file finishes. The committed-blob listing that feeds
  // `progressDone`/`bytes_done` therefore stays flat for minutes while every
  // pod chews through a multi-GB `.nsq` sequence volume — the network is at
  // full speed but the counter and the bytes-derived speed label do not move.
  // Without a hint this reads as a stall, so when the committed count has not
  // advanced for a while during an active copy we surface a reassuring
  // "transferring large volumes…" note instead of a dead-looking frozen row.
  const lastAdvanceMsRef = useRef<number>(Date.now());
  const lastProgressRef = useRef<number>(progressDone);
  useEffect(() => {
    if (!copyActive) {
      lastAdvanceMsRef.current = Date.now();
      lastProgressRef.current = 0;
      return;
    }
    if (progressDone > lastProgressRef.current) {
      lastProgressRef.current = progressDone;
      lastAdvanceMsRef.current = Date.now();
    }
  }, [copyActive, progressDone]);
  const transferringLargeVolumes =
    copyActive &&
    !speedLabel &&
    copyElapsedSeconds > 0 &&
    Date.now() - lastAdvanceMsRef.current > 20_000;
  const unsupported = db.unsupported;
  const isUnsupported = Boolean(unsupported);
  // Suppress the generic "Not in current NCBI snapshot" warning for DBs we
  // already know NCBI never publishes via the S3 mirror — the dedicated
  // unsupported badge carries clearer wording + the real source URL.
  const previewUnavailable =
    !isUnsupported && preview ? preview.available === false : false;
  const ncbiFtpUrl = ncbiBlastDbFtpUrl(db.value, db.type);
  const downloadBlocked =
    isUnsupported ||
    writeDisabled ||
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
      {(isDownloading || copyActive) && (
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
        ) : isDownloading || copyActive ? (
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
          {db.recommended && !isDownloaded && (
            <span
              className="gt gt-g"
              style={{ fontSize: 9, padding: "1px 6px", flexShrink: 0 }}
              title="Recommended starter database for this molecule type"
            >
              Recommended
            </span>
          )}
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
          {!isDownloaded && !isDownloading && !copyActive && (
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
                    <NcbiVersionBadge
                      version={preview.snapshot}
                      href={ncbiFtpUrl}
                      title={`Open ${db.value} metadata on the NCBI BLAST DB FTP server`}
                    />
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
          {copyActive && progressTotal > 0 && (
            <span style={{ color: "var(--accent)" }}>
              Copying {progressDone} / {progressTotal} {progressUnit}{" "}
              {copyElapsedSeconds > 0 && (
                <span
                  style={{
                    fontFamily: "var(--font-mono)",
                    color: "var(--text-faint)",
                  }}
                >
                  · {copyElapsedSeconds}s
                </span>
              )}
              {speedLabel && (
                <span
                  style={{
                    fontFamily: "var(--font-mono)",
                    color: "var(--text-faint)",
                  }}
                >
                  {" "}
                  · {speedLabel}
                </span>
              )}
              {transferringLargeVolumes ? (
                <span
                  style={{ color: "var(--text-faint)" }}
                  title="Each large sequence volume is committed only when its download finishes, so the file counter pauses between commits even though data is still streaming at full speed."
                >
                  {" "}
                  · transferring large volumes…
                </span>
              ) : (
                etaLabel && (
                  <span style={{ color: "var(--text-faint)" }}>
                    {" "}
                    · {etaLabel}
                  </span>
                )
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
                <NcbiVersionBadge
                  version={meta.source_version}
                  href={ncbiFtpUrl}
                  title={`Open ${db.value} metadata on the NCBI BLAST DB FTP server`}
                />
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
              cursor: autoWarmupDisabled || writeDisabled ? "not-allowed" : "pointer",
              opacity: autoWarmupDisabled || writeDisabled ? 0.55 : 1,
            }}
            title={
              writeDisabled
                ? writeDisabledReason
                : autoWarmupDisabled
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
              disabled={autoWarmupDisabled || writeDisabled}
              onChange={(event) => onToggleAutoWarmup(event.target.checked)}
              style={{ accentColor: "var(--success)", margin: 0 }}
            />
            Auto warm
          </label>
        </div>
        {copyActive && progressTotal > 0 && (
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
                width: `${copyPct}%`,
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
        {isDeleting ? (
          // A delete is in flight. Override every other state (Ready /
          // partial-Retry / Get) so the row shows a single, unambiguous
          // "Deleting…" chip and never offers the Get/Retry button — clicking
          // it would race a download against the in-progress blob removal.
          <span
            className="gt gt-b"
            style={{
              fontSize: 10,
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
            }}
          >
            <Loader2 size={10} className="spin" /> Deleting…
          </span>
        ) : isUpdating ? (
          <div style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            <span
              className="gt gt-b"
              style={{
                fontSize: 10,
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
              }}
            >
              <Loader2 size={10} className="spin" /> Updating
              {progressTotal > 0 ? ` · ${copyPct}%` : ""}
            </span>
            {onCancel && (
              <button
                className="glass-button"
                style={{
                  fontSize: 10,
                  padding: "2px 6px",
                  color: "var(--danger)",
                  borderColor: "rgba(224,123,138,0.3)",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 4,
                }}
                onClick={(e) => {
                  e.stopPropagation();
                  onCancel();
                }}
                disabled={writeDisabled || isCancelling}
                title={
                  writeDisabled ? writeDisabledReason : "Cancel in-flight update"
                }
              >
                {isCancelling ? (
                  <>
                    <Loader2 size={10} className="spin" /> Cancelling…
                  </>
                ) : (
                  "Cancel"
                )}
              </button>
            )}
          </div>
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
            disabled={downloadDisabled || writeDisabled}
            title={
              writeDisabled
                ? writeDisabledReason
                : `Update from ${formatNcbiVersion(meta!.source_version!)} to ${formatNcbiVersion(latestVersion!)}`
            }
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
              disabled={oracleDisabled || writeDisabled}
              title={
                writeDisabled
                  ? writeDisabledReason
                  : (oracleDisabledReason ??
                    "Build DB order oracle from warmed AKS shards")
              }
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
            {onDelete && (
              <button
                className="glass-button"
                style={{
                  fontSize: 10,
                  padding: "2px 6px",
                  color: "var(--danger)",
                  borderColor: "rgba(224,123,138,0.3)",
                  display: "inline-flex",
                  alignItems: "center",
                }}
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete();
                }}
                disabled={writeDisabled || isDeleting}
                title={
                  writeDisabled
                    ? writeDisabledReason
                    : "Delete this database (removes all staged blobs)"
                }
                aria-label={`Delete ${db.value}`}
              >
                {isDeleting ? (
                  <Loader2 size={11} className="spin" />
                ) : (
                  <Trash2 size={11} />
                )}
              </button>
            )}
          </div>
        ) : isDownloading ? (
          <span
            className="gt gt-b"
            style={{ fontSize: 10, fontFamily: "var(--font-mono)" }}
          >
            {elapsed}s
          </span>
        ) : copyActive ? (
          <div style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            <span className="gt gt-b" style={{ fontSize: 10 }}>
              {copyPct}%
            </span>
            {onCancel && (
              <button
                className="glass-button"
                style={{
                  fontSize: 10,
                  padding: "2px 6px",
                  color: "var(--danger)",
                  borderColor: "rgba(224,123,138,0.3)",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 4,
                }}
                onClick={(e) => {
                  e.stopPropagation();
                  onCancel();
                }}
                disabled={writeDisabled || isCancelling}
                title={writeDisabled ? writeDisabledReason : "Cancel in-flight download"}
              >
                {isCancelling ? (
                  <>
                    <Loader2 size={10} className="spin" /> Cancelling…
                  </>
                ) : (
                  "Cancel"
                )}
              </button>
            )}
          </div>
        ) : (
          <div style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
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
                writeDisabled
                  ? writeDisabledReason
                  : isUnsupported && unsupported
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
            {onDelete && (isPartial || copyPhase === "cancelled") && (
              <button
                className="glass-button"
                style={{
                  fontSize: 10,
                  padding: "2px 6px",
                  color: "var(--danger)",
                  borderColor: "rgba(224,123,138,0.3)",
                  display: "inline-flex",
                  alignItems: "center",
                }}
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete();
                }}
                disabled={writeDisabled || isDeleting}
                title={
                  writeDisabled
                    ? writeDisabledReason
                    : "Delete this database (removes all staged blobs)"
                }
                aria-label={`Delete ${db.value}`}
              >
                {isDeleting ? (
                  <Loader2 size={11} className="spin" />
                ) : (
                  <Trash2 size={11} />
                )}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function NcbiVersionBadge({
  version,
  href,
  title,
}: {
  version: string;
  href: string;
  title: string;
}) {
  const content = (
    <>
      <code
        style={{
          fontSize: 10,
          background: "var(--bg-tertiary)",
          padding: "1px 5px",
          borderRadius: 3,
        }}
      >
        v:{formatNcbiVersion(version)}
      </code>
      <ExternalLink size={10} strokeWidth={1.5} aria-hidden="true" />
    </>
  );
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title={title}
      aria-label={`Open NCBI BLAST DB metadata for ${formatNcbiVersion(version)} in a new tab`}
      onClick={(event) => event.stopPropagation()}
      style={{
        color: "inherit",
        textDecoration: "none",
        display: "inline-flex",
        alignItems: "center",
        gap: 3,
      }}
    >
      {content}
    </a>
  );
}
