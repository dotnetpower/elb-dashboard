import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { Database, Lock, RefreshCw, X } from "lucide-react";

import { StorageDownloadResultBanner } from "@/components/cards/StorageDownloadResultBanner";
import {
  DB_CATALOG,
  formatBytes,
  formatNcbiVersion,
} from "@/components/cards/storageDbCatalog";
import { BlastDbCustomInput } from "@/components/cards/storage/BlastDbCustomInput";
import { BlastDbLargeConfirm } from "@/components/cards/storage/BlastDbLargeConfirm";
import { BlastDbRow } from "@/components/cards/storage/BlastDbRow";
import { BlastDbUpdateConfirm } from "@/components/cards/storage/BlastDbUpdateConfirm";
import {
  readAutoWarmupDbs,
  setAutoWarmupDb,
} from "@/components/cards/storage/autoWarmupPrefs";
import type { UseBlastDbReturn } from "@/components/cards/storage/useBlastDb";
import { useDbPreviews } from "@/components/cards/storage/useDbPreviews";

const CATEGORIES = ["Small / Test", "Medium", "Large"] as const;

interface BlastDbModalProps {
  state: UseBlastDbReturn;
  onClose: () => void;
}

/**
 * Full-screen popup that lists every catalog DB grouped by size category and
 * lets the user trigger downloads. Renders into `document.body` via portal so
 * it always escapes ancestor `overflow` clipping.
 *
 * All lifecycle state (download/in-progress/completed) is owned by the parent
 * via `useBlastDb`; this component is the rendering layer for the modal only.
 */
export function BlastDbModal({ state, onClose }: BlastDbModalProps) {
  const [confirmLargeDb, setConfirmLargeDb] = useState<string | null>(null);
  const [confirmUpdateDb, setConfirmUpdateDb] = useState<string | null>(null);
  const [autoWarmupDbs, setAutoWarmupDbs] = useState<Set<string>>(() =>
    readAutoWarmupDbs(),
  );

  // ESC key closes
  useEffect(() => {
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        setConfirmLargeDb(null);
        setConfirmUpdateDb(null);
      }
    };
    window.addEventListener("keydown", handleEsc);
    return () => window.removeEventListener("keydown", handleEsc);
  }, [onClose]);

  // Lock body scroll while modal is open
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  const {
    dbQuery,
    latestVersion,
    publicAccessDisabled,
    canEnableLocalAccess,
    openingLocalDebug,
    enableLocalAccess,
    storageAccessTitle,
    storageAccessHint,
    downloadedDbs,
    isDbReady,
    updatesAvailable,
    updatesAvailableByDb,
    downloading,
    oracleBuilding,
    inProgress,
    elapsed,
    downloadResult,
    dismissDownloadResult,
    handleDownload,
    handleBuildOracle,
    handleCancel,
  } = state;

  // Preview NCBI snapshot info for every catalog row + the custom-input
  // value so the user sees real file count / size / last-modified BEFORE
  // clicking Download — and gets an honest "not on S3" hint when a DB is
  // FTP-only or mid-publish.
  //
  // ``skipReady`` suppresses the preview HEAD for DBs whose storage metadata
  // already carries everything we need (file_count, total_bytes,
  // source_version). For a deployment whose catalog is mostly downloaded
  // this saves N HEAD round trips per modal open against NCBI.
  // Catalog entries flagged as ``unsupported`` are skipped entirely — the
  // dedicated badge already explains why and points at the real source.
  const previewNames = useMemo(
    () => DB_CATALOG.filter((d) => !d.unsupported).map((d) => d.value),
    [],
  );
  const skipReady = useMemo(() => {
    const skip = new Set<string>();
    for (const item of DB_CATALOG) {
      if (isDbReady(downloadedDbs.get(item.value))) skip.add(item.value);
    }
    return skip;
  }, [downloadedDbs, isDbReady]);
  const { byName: previewByName } = useDbPreviews(previewNames, true, skipReady);

  const startDownload = (name: string) => {
    void handleDownload(name);
    setConfirmLargeDb(null);
    setConfirmUpdateDb(null);
  };

  const startUpdate = (name: string) => {
    void state.handleUpdate(name);
    setConfirmUpdateDb(null);
    setConfirmLargeDb(null);
  };

  const toggleAutoWarmup = (name: string, checked: boolean) => {
    setAutoWarmupDbs(setAutoWarmupDb(name, checked));
  };

  const degraded = (dbQuery.data as { degraded?: boolean; message?: string } | undefined)
    ?.degraded;
  const degradedMsg = (dbQuery.data as { message?: string } | undefined)?.message;

  return createPortal(
    <div
      className="glass-dialog-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      role="dialog"
      aria-modal="true"
      aria-label="BLAST Databases"
    >
      <div
        className="glass-card glass-card--strong glass-dialog"
        onClick={(e) => e.stopPropagation()}
        style={{
          maxWidth: 900,
          width: "calc(100vw - 48px)",
          maxHeight: "86vh",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: "var(--space-3)",
          }}
        >
          <h3 style={{ margin: 0, display: "flex", alignItems: "center", gap: 8 }}>
            <Database size={18} strokeWidth={1.5} /> BLAST Databases
          </h3>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <button
              className="glass-button"
              onClick={() => dbQuery.refetch()}
              disabled={dbQuery.isFetching}
              style={{ padding: "4px 6px", border: "none" }}
              title="Refresh database status"
            >
              <RefreshCw
                size={14}
                strokeWidth={1.5}
                className={dbQuery.isFetching ? "spin" : ""}
              />
            </button>
            <button
              className="glass-button"
              onClick={onClose}
              style={{ padding: "4px 6px", border: "none" }}
              title="Close"
            >
              <X size={16} strokeWidth={1.5} />
            </button>
          </div>
        </div>

        <div
          style={{
            display: "flex",
            gap: 12,
            marginBottom: "var(--space-3)",
            fontSize: 11,
            color: "var(--text-muted)",
            flexWrap: "wrap",
          }}
        >
          {(() => {
            const readyDbs = [...downloadedDbs.values()].filter((d) => isDbReady(d));
            return (
              <>
                <span>
                  {readyDbs.length}/{DB_CATALOG.length} downloaded
                </span>
                {readyDbs.length > 0 && (
                  <span>
                    {formatBytes(
                      readyDbs.reduce((s, d) => s + (d.total_bytes ?? 0), 0),
                    )}{" "}
                    used
                  </span>
                )}
              </>
            );
          })()}
          {updatesAvailable > 0 && (
            <span style={{ color: "var(--warning)", fontWeight: 600 }}>
              {updatesAvailable} update{updatesAvailable > 1 ? "s" : ""} available
            </span>
          )}
          {latestVersion && (
            <span>
              NCBI latest:{" "}
              <code style={{ fontSize: 9 }}>{formatNcbiVersion(latestVersion)}</code>
            </span>
          )}
          <span style={{ display: "flex", alignItems: "center", gap: 3 }}>
            <span className="gt gt-b" style={{ fontSize: 8 }}>
              N
            </span>{" "}
            = nucleotide
          </span>
          <span style={{ display: "flex", alignItems: "center", gap: 3 }}>
            <span className="gt gt-p" style={{ fontSize: 8 }}>
              P
            </span>{" "}
            = protein
          </span>
        </div>

        {downloadResult && (
          <StorageDownloadResultBanner
            result={downloadResult}
            onDismiss={dismissDownloadResult}
          />
        )}

        {degraded && (
          <div
            style={{
              padding: "8px 12px",
              marginBottom: "var(--space-3)",
              borderRadius: 6,
              fontSize: 11,
              background: "rgba(224,123,138,0.08)",
              border: "1px solid rgba(224,123,138,0.2)",
              color: "var(--danger)",
              lineHeight: 1.5,
            }}
          >
            <strong>Cannot read databases from storage:</strong>{" "}
            {degradedMsg ?? "Storage access denied. Check RBAC roles."}
          </div>
        )}

        {publicAccessDisabled && (
          <div
            style={{
              padding: "8px 12px",
              marginBottom: "var(--space-3)",
              borderRadius: 6,
              fontSize: 11,
              background: "rgba(240,198,116,0.08)",
              border: "1px solid rgba(240,198,116,0.2)",
              color: "var(--warning)",
              display: "flex",
              alignItems: "flex-start",
              gap: 10,
            }}
          >
            <Lock size={14} style={{ flexShrink: 0, marginTop: 1 }} />
            <div style={{ flex: 1 }}>
              <strong>{storageAccessTitle}.</strong> {storageAccessHint} Downloaded state
              cannot be detected until Storage accepts the local data-plane request. The
              catalog below still shows all available databases.
            </div>
            {canEnableLocalAccess && (
              <button
                className="glass-button glass-button--primary"
                disabled={openingLocalDebug}
                onClick={async () => {
                  await enableLocalAccess();
                }}
                style={{
                  padding: "5px 10px",
                  fontSize: 11,
                  whiteSpace: "nowrap",
                }}
                title="Open the storage account to your IP for local debugging"
              >
                {openingLocalDebug ? "Opening…" : "Enable for local debug"}
              </button>
            )}
          </div>
        )}

        <div style={{ overflowY: "auto", flex: 1, paddingRight: 4 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {CATEGORIES.map((cat) => {
              const dbs = DB_CATALOG.filter((d) => d.category === cat);
              const downloadedInCat = dbs.filter((d) =>
                isDbReady(downloadedDbs.get(d.value)),
              ).length;
              return (
                <div key={cat}>
                  <div
                    style={{
                      position: "sticky",
                      top: 0,
                      zIndex: 1,
                      background: "var(--bg-secondary)",
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      padding: "8px 4px 6px",
                      marginBottom: 2,
                      borderBottom: "1px solid var(--border-weak)",
                    }}
                  >
                    <span
                      style={{
                        fontSize: 11,
                        fontWeight: 600,
                        color: "var(--text-muted)",
                        textTransform: "uppercase",
                        letterSpacing: "0.08em",
                      }}
                    >
                      {cat}
                    </span>
                    <span style={{ fontSize: 10, color: "var(--text-faint)" }}>
                      · {downloadedInCat}/{dbs.length}
                    </span>
                    {cat === "Large" && (
                      <span
                        className="gt gt-o"
                        style={{
                          fontSize: 9,
                          marginLeft: "auto",
                          fontWeight: 600,
                        }}
                      >
                        May take hours
                      </span>
                    )}
                  </div>
                  {dbs.map((db) => {
                    const inProgressInfo = inProgress.get(db.value);
                    const isCopying = Boolean(inProgressInfo);
                    const meta = downloadedDbs.get(db.value);
                    const isDownloaded = !isCopying && isDbReady(meta);
                    const isDownloading = downloading === db.value;
                    const preview = previewByName.get(db.value);
                    const expectedFromPreview =
                      preview?.file_count ?? inProgressInfo?.expectedFiles ?? 0;
                    const copyProgress = inProgressInfo
                      ? Math.min(
                          100,
                          Math.round(
                            ((meta?.copy_status?.success ?? meta?.file_count ?? 0) /
                              Math.max(
                                meta?.copy_status?.total_files ??
                                  inProgressInfo.expectedFiles ??
                                  expectedFromPreview ??
                                  1,
                                1,
                              )) *
                              100,
                          ),
                        )
                      : 0;
                    // Per-DB update detection prefers the server-side ETag
                    // map; fall back to the legacy snapshot comparison only
                    // when the server omitted the per-DB list.
                    const etagUpdate = updatesAvailableByDb.has(db.value);
                    const legacyUpdate =
                      !!meta?.source_version &&
                      !!latestVersion &&
                      meta.source_version !== latestVersion;
                    const hasUpdate =
                      isDownloaded &&
                      (etagUpdate || (updatesAvailableByDb.size === 0 && legacyUpdate)) &&
                      !meta?.update_in_progress;
                    return (
                      <BlastDbRow
                        key={db.value}
                        db={db}
                        meta={meta}
                        preview={preview}
                        isDownloaded={isDownloaded}
                        isDownloading={isDownloading}
                        isCopying={isCopying}
                        inProgressInfo={inProgressInfo}
                        copyProgress={copyProgress}
                        hasUpdate={hasUpdate}
                        latestVersion={latestVersion}
                        elapsed={elapsed}
                        downloadDisabled={downloading !== null}
                        oracleBuilding={oracleBuilding === db.value}
                        oracleDisabled={!isDownloaded || oracleBuilding !== null}
                        autoWarmupChecked={autoWarmupDbs.has(db.value)}
                        autoWarmupDisabled={
                          !isDownloaded || hasUpdate || !!meta?.update_in_progress
                        }
                        onDownload={() => startDownload(db.value)}
                        onUpdate={() => setConfirmUpdateDb(db.value)}
                        onBuildOracle={() => void handleBuildOracle(db.value)}
                        onConfirmLarge={() => setConfirmLargeDb(db.value)}
                        onCancel={() => void handleCancel(db.value)}
                        onToggleAutoWarmup={(checked) =>
                          toggleAutoWarmup(db.value, checked)
                        }
                      />
                    );
                  })}
                </div>
              );
            })}
          </div>

          <div style={{ marginTop: "var(--space-2)" }}>
            <BlastDbCustomInput
              disabled={downloading !== null}
              onDownload={(name) => startDownload(name)}
            />
          </div>

          {confirmLargeDb && (
            <BlastDbLargeConfirm
              dbValue={confirmLargeDb}
              onConfirm={() => startDownload(confirmLargeDb)}
              onCancel={() => setConfirmLargeDb(null)}
            />
          )}

          {confirmUpdateDb && (
            <BlastDbUpdateConfirm
              dbValue={confirmUpdateDb}
              meta={downloadedDbs.get(confirmUpdateDb)}
              latestVersion={latestVersion}
              onConfirm={() => startUpdate(confirmUpdateDb)}
              onCancel={() => setConfirmUpdateDb(null)}
            />
          )}

          <div className="muted" style={{ fontSize: 10, marginTop: "var(--space-2)" }}>
            Server-side copy from NCBI S3 → <code style={{ fontSize: 10 }}>blast-db</code>{" "}
            container. No local download required.
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}
