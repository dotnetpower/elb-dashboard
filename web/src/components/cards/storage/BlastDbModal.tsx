import { useEffect, useMemo, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Database, Loader2, Lock, RefreshCw, ShieldCheck, X } from "lucide-react";

import { useToast } from "@/components/Toast";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { StorageDownloadResultBanner } from "@/components/cards/StorageDownloadResultBanner";
import {
  DB_CATALOG,
  MOLECULE_PROGRAMS,
  type MoleculeFilter,
  countUnavailableDbs,
  filterDbCatalog,
  formatBytes,
  formatNcbiVersion,
} from "@/components/cards/storageDbCatalog";
import { BlastDbCustomInput } from "@/components/cards/storage/BlastDbCustomInput";
import {
  BlastDbClusterConfirm,
  type BlastDbClusterTopology,
  shouldConfirmDownloadBeforeAks,
} from "@/components/cards/storage/BlastDbClusterConfirm";
import { BlastDbLargeConfirm } from "@/components/cards/storage/BlastDbLargeConfirm";
import {
  BlastDbRow,
  BlastDbRowSkeleton,
} from "@/components/cards/storage/BlastDbRow";
import { BlastDbUpdateConfirm } from "@/components/cards/storage/BlastDbUpdateConfirm";
import { dbHasUpdate } from "@/components/cards/storage/blastDbUpdates";
import {
  readAutoWarmupDbs,
  setAutoWarmupDb,
} from "@/components/cards/storage/autoWarmupPrefs";
import type { UseBlastDbReturn } from "@/components/cards/storage/useBlastDb";
import { useDbPreviews } from "@/components/cards/storage/useDbPreviews";

const CATEGORIES = ["Small / Test", "Medium", "Large"] as const;

/**
 * Centered overlay wrapper rendered into `document.body`. The download / update
 * confirm panels are otherwise appended to the bottom of the long scrollable
 * catalog list, so clicking "Update" on a row near the top would render the
 * confirmation off-screen and look like nothing happened. Wrapping them in a
 * backdrop overlay keeps the confirmation in view regardless of scroll position.
 */
function ConfirmOverlay({
  onDismiss,
  children,
}: {
  onDismiss: () => void;
  children: ReactNode;
}) {
  return createPortal(
    <div
      className="glass-dialog-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onDismiss();
      }}
      role="dialog"
      aria-modal="true"
    >
      <div
        className="glass-card glass-card--strong glass-dialog"
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: 640, width: "calc(100vw - 48px)" }}
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}

interface BlastDbModalProps {
  state: UseBlastDbReturn;
  clusterTopology?: BlastDbClusterTopology;
  /**
   * When true the signed-in user only holds a read role (e.g. subscription
   * Reader) at the storage scope, so every write action (Get / Update /
   * Build Oracle / Cancel / Auto warm / Custom DB) is rendered disabled with
   * ``writeDisabledReason`` as the tooltip. Defaults to false (degrade-open).
   */
  writeDisabled?: boolean;
  writeDisabledReason?: string;
  /**
   * False when the target AKS workload cluster is not Running. The Build
   * Oracle Jobs execute on the warmed cluster nodes, so a stopped cluster
   * makes the build fail — the button is disabled with an explanatory
   * tooltip. Defaults to true (degrade-open while AKS status is unknown).
   */
  clusterReady?: boolean;
  onClose: () => void;
}

interface PendingDownloadConfirm {
  dbValue: string;
  isLarge: boolean;
}

/**
 * Full-screen popup that lists every catalog DB grouped by size category and
 * lets the user trigger downloads. Renders into `document.body` via portal so
 * it always escapes ancestor `overflow` clipping.
 *
 * All lifecycle state (download/in-progress/completed) is owned by the parent
 * via `useBlastDb`; this component is the rendering layer for the modal only.
 */
export function BlastDbModal({
  state,
  clusterTopology,
  writeDisabled = false,
  writeDisabledReason,
  clusterReady = true,
  onClose,
}: BlastDbModalProps) {
  const [confirmClusterDb, setConfirmClusterDb] = useState<PendingDownloadConfirm | null>(
    null,
  );
  const [confirmLargeDb, setConfirmLargeDb] = useState<string | null>(null);
  const [confirmUpdateDb, setConfirmUpdateDb] = useState<string | null>(null);
  const [confirmCancelDb, setConfirmCancelDb] = useState<string | null>(null);
  const [confirmDeleteDb, setConfirmDeleteDb] = useState<string | null>(null);
  const [autoWarmupDbs, setAutoWarmupDbs] = useState<Set<string>>(() =>
    readAutoWarmupDbs(),
  );
  // Program-oriented filter: blastn/tblastn/tblastx need a nucleotide DB,
  // blastp/blastx need a protein DB. `showUnavailable` is off by default so the
  // list only shows databases the server-side S3 copy can actually `Get`.
  const [moleculeFilter, setMoleculeFilter] = useState<MoleculeFilter>("all");
  const [showUnavailable, setShowUnavailable] = useState(false);
  const { toast } = useToast();

  // ESC key closes
  useEffect(() => {
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        setConfirmClusterDb(null);
        setConfirmLargeDb(null);
        setConfirmUpdateDb(null);
        setConfirmCancelDb(null);
        setConfirmDeleteDb(null);
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
    canGrantLocalRbac,
    openingLocalDebug,
    grantingLocalRbac,
    enableLocalAccess,
    grantLocalRbac,
    storageAccessTitle,
    storageAccessHint,
    downloadedDbs,
    isDbReady,
    updatesAvailable,
    updatesAvailableByDb,
    updatesEvaluated,
    downloading,
    oracleBuilding,
    inProgress,
    elapsed,
    pendingAction,
    downloadResult,
    dismissDownloadResult,
    handleDownload,
    handleBuildOracle,
    handleCancel,
    handleDelete,
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
    setConfirmClusterDb(null);
    setConfirmLargeDb(null);
    setConfirmUpdateDb(null);
  };

  const requestDownload = (name: string, isLarge: boolean) => {
    if (shouldConfirmDownloadBeforeAks(clusterTopology)) {
      setConfirmClusterDb({ dbValue: name, isLarge });
      setConfirmLargeDb(null);
      setConfirmUpdateDb(null);
      return;
    }
    if (isLarge) {
      setConfirmLargeDb(name);
      setConfirmClusterDb(null);
      setConfirmUpdateDb(null);
      return;
    }
    startDownload(name);
  };

  const startUpdate = (name: string) => {
    void state.handleUpdate(name);
    setConfirmClusterDb(null);
    setConfirmUpdateDb(null);
    setConfirmLargeDb(null);
  };

  const toggleAutoWarmup = (name: string, checked: boolean) => {
    setAutoWarmupDbs(setAutoWarmupDb(name, checked));
  };

  const degraded = (dbQuery.data as { degraded?: boolean; message?: string } | undefined)
    ?.degraded;
  const degradedMsg = (dbQuery.data as { message?: string } | undefined)?.message;

  // While the database list is still loading for the first time the
  // downloaded-state map is empty, so every catalog row would otherwise
  // render an actionable "Get" button (and Update/Delete look reachable too).
  // Show shimmer placeholders instead until the real state is known.
  const dbInitialLoading = dbQuery.isLoading;

  const visibleDbCount = useMemo(
    () => filterDbCatalog(DB_CATALOG, moleculeFilter, showUnavailable).length,
    [moleculeFilter, showUnavailable],
  );

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
                    {formatBytes(readyDbs.reduce((s, d) => s + (d.total_bytes ?? 0), 0))}{" "}
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

        {/* Program-oriented molecule filter + unavailable toggle. */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            marginBottom: "var(--space-3)",
            flexWrap: "wrap",
          }}
        >
          <div className="db-filter-tabs" role="tablist" aria-label="Filter databases by molecule type">
            {(
              [
                { key: "all", label: "All" },
                { key: "nucl", label: "Nucleotide" },
                { key: "prot", label: "Protein" },
              ] as const
            ).map((tab) => {
              const active = moleculeFilter === tab.key;
              const programs =
                tab.key === "all" ? null : MOLECULE_PROGRAMS[tab.key].join(" · ");
              return (
                <button
                  key={tab.key}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  className={`db-filter-tab${active ? " db-filter-tab--active" : ""}`}
                  onClick={() => setMoleculeFilter(tab.key)}
                  title={
                    programs ? `Databases for ${programs}` : "Show every database"
                  }
                >
                  {tab.label}
                  {programs && (
                    <span className="db-filter-tab__programs">{programs}</span>
                  )}
                </button>
              );
            })}
          </div>
          {(() => {
            const hiddenCount = countUnavailableDbs(DB_CATALOG, moleculeFilter);
            if (hiddenCount === 0) return null;
            return (
              <label
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  fontSize: 11,
                  color: "var(--text-muted)",
                  cursor: "pointer",
                  marginLeft: "auto",
                }}
                title="Show databases NCBI does not publish as a pullable BLAST DB (v4-only, no prebuilt, or too large)"
              >
                <input
                  type="checkbox"
                  checked={showUnavailable}
                  onChange={(e) => setShowUnavailable(e.target.checked)}
                />
                Show unavailable ({hiddenCount})
              </label>
            );
          })()}
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
              display: "flex",
              alignItems: "center",
              gap: 10,
            }}
          >
            <div style={{ flex: 1 }}>
              <strong>Cannot read databases from storage:</strong>{" "}
              {degradedMsg ?? "Storage access denied. Check RBAC roles."}
            </div>
            {canGrantLocalRbac && (
              <button
                className="glass-button glass-button--primary"
                disabled={grantingLocalRbac}
                onClick={async () => {
                  const result = await grantLocalRbac();
                  toast(result.message, result.ok ? "success" : "error");
                }}
                style={{
                  padding: "5px 10px",
                  fontSize: 11,
                  whiteSpace: "nowrap",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                }}
                title="Grant Storage RBAC for this local dashboard session"
              >
                {grantingLocalRbac ? (
                  <Loader2 size={12} className="spin" />
                ) : (
                  <ShieldCheck size={12} strokeWidth={1.8} />
                )}
                {grantingLocalRbac ? "Granting…" : "Grant local RBAC"}
              </button>
            )}
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
            {dbInitialLoading &&
              Array.from({ length: 6 }).map((_, i) => (
                <BlastDbRowSkeleton key={`db-skeleton-${i}`} />
              ))}
            {!dbInitialLoading && visibleDbCount === 0 && (
              <div
                className="muted"
                style={{ fontSize: 12, padding: "16px 4px", textAlign: "center" }}
              >
                No databases match this filter. Enable “Show unavailable” to see
                databases NCBI does not publish as a pullable BLAST DB.
              </div>
            )}
            {!dbInitialLoading &&
              CATEGORIES.map((cat) => {
              const dbs = filterDbCatalog(
                DB_CATALOG.filter((d) => d.category === cat),
                moleculeFilter,
                showUnavailable,
              );
              if (dbs.length === 0) return null;
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
                    // Per-DB update detection prefers the server-side ETag
                    // map; fall back to the legacy snapshot comparison only
                    // when the server did NOT evaluate per-DB (no storage
                    // scope / list failed). See dbHasUpdate for the rule.
                    const hasUpdate = dbHasUpdate({
                      meta,
                      isDownloaded,
                      inUpdateMap: updatesAvailableByDb.has(db.value),
                      updatesEvaluated,
                      latestVersion,
                    });
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
                        hasUpdate={hasUpdate}
                        latestVersion={latestVersion}
                        elapsed={elapsed}
                        downloadDisabled={downloading !== null}
                        oracleBuilding={oracleBuilding === db.value}
                        oracleDisabled={
                          !isDownloaded || oracleBuilding !== null || !clusterReady
                        }
                        oracleDisabledReason={
                          !clusterReady
                            ? "AKS cluster is not running — start it before building the order oracle"
                            : undefined
                        }
                        autoWarmupChecked={autoWarmupDbs.has(db.value)}
                        autoWarmupDisabled={
                          !isDownloaded || hasUpdate || !!meta?.update_in_progress
                        }
                        writeDisabled={writeDisabled}
                        writeDisabledReason={writeDisabledReason}
                        onDownload={() => requestDownload(db.value, false)}
                        onUpdate={() => setConfirmUpdateDb(db.value)}
                        onBuildOracle={() => void handleBuildOracle(db.value)}
                        onConfirmLarge={() => requestDownload(db.value, true)}
                        onCancel={() => setConfirmCancelDb(db.value)}
                        isCancelling={pendingAction.get(db.value) === "cancel"}
                        onDelete={() => setConfirmDeleteDb(db.value)}
                        isDeleting={pendingAction.get(db.value) === "delete"}
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
              disabled={dbInitialLoading || downloading !== null || writeDisabled}
              writeDisabledReason={writeDisabled ? writeDisabledReason : undefined}
              onDownload={(name) => requestDownload(name, false)}
            />
          </div>

          {confirmClusterDb && (
            <ConfirmOverlay onDismiss={() => setConfirmClusterDb(null)}>
              <BlastDbClusterConfirm
                dbValue={confirmClusterDb.dbValue}
                isLarge={confirmClusterDb.isLarge}
                topology={clusterTopology}
                onConfirm={() => startDownload(confirmClusterDb.dbValue)}
                onCancel={() => setConfirmClusterDb(null)}
              />
            </ConfirmOverlay>
          )}

          {confirmLargeDb && (
            <ConfirmOverlay onDismiss={() => setConfirmLargeDb(null)}>
              <BlastDbLargeConfirm
                dbValue={confirmLargeDb}
                onConfirm={() => startDownload(confirmLargeDb)}
                onCancel={() => setConfirmLargeDb(null)}
              />
            </ConfirmOverlay>
          )}

          {confirmUpdateDb && (
            <ConfirmOverlay onDismiss={() => setConfirmUpdateDb(null)}>
              <BlastDbUpdateConfirm
                dbValue={confirmUpdateDb}
                meta={downloadedDbs.get(confirmUpdateDb)}
                latestVersion={latestVersion}
                onConfirm={() => startUpdate(confirmUpdateDb)}
                onCancel={() => setConfirmUpdateDb(null)}
              />
            </ConfirmOverlay>
          )}

          {confirmCancelDb && (
            <ConfirmDialog
              title={`Cancel download of ${confirmCancelDb}?`}
              message={
                "Files already copied to the blast-db container stay in place; " +
                "only the remaining (pending) copies are aborted. You can " +
                "restart the download later to finish the rest."
              }
              confirmLabel="Cancel download"
              confirmAriaLabel={`Cancel the in-flight download of ${confirmCancelDb}`}
              tone="danger"
              onConfirm={() => {
                const dbValue = confirmCancelDb;
                setConfirmCancelDb(null);
                void handleCancel(dbValue);
              }}
              onCancel={() => setConfirmCancelDb(null)}
            />
          )}

          {confirmDeleteDb && (
            <ConfirmDialog
              title={`Delete ${confirmDeleteDb}?`}
              message={
                "This permanently removes every staged shard blob and the " +
                "database metadata from the blast-db container. Any AKS " +
                "prepare-db Job left over is deleted too. This cannot be " +
                "undone — you would have to download the database again."
              }
              confirmLabel="Delete database"
              confirmAriaLabel={`Permanently delete ${confirmDeleteDb}`}
              tone="danger"
              onConfirm={() => {
                const dbValue = confirmDeleteDb;
                setConfirmDeleteDb(null);
                void handleDelete(dbValue);
              }}
              onCancel={() => setConfirmDeleteDb(null)}
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
