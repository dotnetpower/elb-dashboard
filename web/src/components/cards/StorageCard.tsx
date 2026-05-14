import { useState, useEffect, useMemo, useRef } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Database,
  Loader2,
  CheckCircle2,
  AlertTriangle,
  Lock,
  Globe,
  ShieldAlert,
  Download,
  Circle,
  Maximize2,
  X,
  RefreshCw,
} from "lucide-react";

import { api, formatApiError } from "@/api/client";
import { monitoringApi, blastApi } from "@/api/endpoints";
import { MonitorCard } from "@/components/MonitorCard";
import { StorageDownloadResultBanner } from "@/components/cards/StorageDownloadResultBanner";
import {
  DB_CATALOG,
  formatBytes,
  formatNcbiVersion,
  formatStorageDate,
} from "@/components/cards/storageDbCatalog";

const HNS_DISMISSED_KEY = "elb-hns-warning-dismissed";

interface Props {
  subscriptionId: string;
  resourceGroup: string;
  accountName: string;
}

export function StorageCard({ subscriptionId, resourceGroup, accountName }: Props) {
  const enabled = Boolean(subscriptionId && resourceGroup && accountName);
  const queryClient = useQueryClient();
  const queryKey = ["storage", subscriptionId, resourceGroup, accountName];

  const query = useQuery({
    queryKey,
    queryFn: () => monitoringApi.storage(subscriptionId, resourceGroup, accountName),
    enabled,
    refetchInterval: 30_000,
  });

  // --- Public access toggle ---
  const [showConfirmEnable, setShowConfirmEnable] = useState(false);
  const [keepAlive, setKeepAlive] = useState(() => {
    try {
      const stored = localStorage.getItem("elb-storage-keep-alive");
      return stored === null ? true : stored === "1";
    } catch {
      return true;
    }
  });
  const [toggleMsg, setToggleMsg] = useState<{ type: "ok" | "err"; text: string } | null>(
    null,
  );
  const [hnsDismissed, setHnsDismissed] = useState(() => {
    try {
      return localStorage.getItem(HNS_DISMISSED_KEY) === "1";
    } catch {
      return false;
    }
  });

  const toggle = useMutation({
    mutationFn: (next: boolean) =>
      api.post<{ public_network_access: string | null }>(
        "/monitor/storage/public-access",
        {
          subscription_id: subscriptionId,
          resource_group: resourceGroup,
          account_name: accountName,
          enabled: next,
        },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
      setToggleMsg({
        type: "ok",
        text: "Change applied. Propagation may take a few seconds.",
      });
    },
    onError: (e) => {
      setToggleMsg({ type: "err", text: formatApiError(e, "storage") });
    },
  });

  // #13: Auto-dismiss toggle message after 5s
  useEffect(() => {
    if (!toggleMsg) return;
    const t = setTimeout(() => setToggleMsg(null), 5000);
    return () => clearTimeout(t);
  }, [toggleMsg]);

  // Keep-alive: when enabled, check every 30s and re-enable if tenant policy disabled it
  const queryRef = useRef(query);
  queryRef.current = query;
  const toggleRef = useRef(toggle);
  toggleRef.current = toggle;

  useEffect(() => {
    if (!keepAlive || !enabled) return;
    const check = () => {
      queryRef.current.refetch().then(() => {
        const cur = queryRef.current.data?.public_network_access;
        if (cur && cur !== "Enabled" && !toggleRef.current.isPending) {
          toggleRef.current.mutate(true);
        }
      });
    };
    check(); // immediate
    const interval = setInterval(check, 30_000);
    return () => clearInterval(interval);
  }, [keepAlive, enabled]);

  const handleKeepAliveToggle = () => {
    const next = !keepAlive;
    setKeepAlive(next);
    try {
      localStorage.setItem("elb-storage-keep-alive", next ? "1" : "0");
    } catch {
      /* noop */
    }
    if (next) {
      const cur = queryRef.current.data?.public_network_access;
      if (cur && cur !== "Enabled" && !toggleRef.current.isPending) {
        toggleRef.current.mutate(true);
      }
    }
  };

  // --- Prepare DB (state moved to BlastDbSection, but we track 'downloading' here for shimmer) ---
  const [dbDownloading, setDbDownloading] = useState<string | null>(null);

  const status = !enabled
    ? "idle"
    : query.isLoading
      ? "loading"
      : query.isError
        ? "error"
        : "ok";
  const publicAccess = query.data?.public_network_access ?? null;
  const isPublic = publicAccess === "Enabled";

  const handleToggleClick = (next: boolean) => {
    if (next) {
      // #14: Confirm before enabling public access
      setShowConfirmEnable(true);
    } else {
      toggle.mutate(false);
    }
  };

  return (
    <MonitorCard
      title="Storage Account"
      subtitle={enabled ? `${accountName} · ${resourceGroup}` : "Configure account name"}
      status={status}
      fetching={query.isFetching || dbDownloading !== null}
      lastRefreshed={query.dataUpdatedAt ? new Date(query.dataUpdatedAt) : null}
      onRefresh={() => query.refetch()}
      accentColor="storage"
      collapsible
      rightSlot={
        enabled && (
          <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
            {/* #17: Smaller toggle button */}
            <button
              className={`glass-button ${isPublic ? "" : "glass-button--primary"}`}
              onClick={() => handleToggleClick(!isPublic)}
              disabled={toggle.isPending}
              style={{ fontSize: 10, padding: "3px 8px" }}
              title={
                isPublic
                  ? "Disable public access (recommended when not running BLAST)"
                  : "Enable public access (required for BLAST searches)"
              }
            >
              {isPublic ? <Lock size={10} /> : <Globe size={10} />}
              {isPublic ? "Lock" : "Unlock"}
            </button>
            {/* Keep-alive toggle */}
            <button
              className={`glass-button ${keepAlive ? "glass-button--primary" : ""}`}
              onClick={handleKeepAliveToggle}
              style={{ fontSize: 10, padding: "3px 8px" }}
              title={
                keepAlive
                  ? "Auto-keep-enabled is ON — public access will be re-enabled if a tenant policy disables it"
                  : "Enable auto-keep — automatically re-enables public access every 30s if it gets disabled"
              }
            >
              <RefreshCw size={10} />
              {keepAlive ? "Auto ✓" : "Auto"}
            </button>
          </div>
        )
      }
    >
      {!enabled && (
        <div className="muted">
          Set Subscription ID, Workload RG, and Storage Account above.
        </div>
      )}
      {query.isError && (
        <div className="muted" style={{ color: "var(--danger)" }}>
          Failed to load storage: {formatApiError(query.error, "storage")}
        </div>
      )}
      {query.data && (
        <>
          {/* #5: Public access warning banner */}
          {isPublic && (
            <div
              style={{
                padding: "6px 10px",
                marginBottom: "var(--space-3)",
                background: "rgba(240,198,116,0.08)",
                border: "1px solid rgba(240,198,116,0.2)",
                borderRadius: 6,
                fontSize: 11,
                color: "var(--warning)",
                display: "flex",
                alignItems: "center",
                gap: 6,
              }}
            >
              <ShieldAlert size={13} strokeWidth={1.5} />
              Public network access is enabled. Disable after BLAST operations complete.
            </div>
          )}

          {/* HNS disabled warning — dismissible */}
          {!query.data.is_hns_enabled && !hnsDismissed && (
            <div
              style={{
                padding: "6px 10px",
                marginBottom: "var(--space-3)",
                background: "rgba(240,198,116,0.08)",
                border: "1px solid rgba(240,198,116,0.2)",
                borderRadius: 6,
                fontSize: 11,
                color: "var(--warning)",
                display: "flex",
                alignItems: "center",
                gap: 6,
              }}
            >
              <AlertTriangle size={13} strokeWidth={1.5} />
              <span style={{ flex: 1 }}>
                HNS (Data Lake Gen2) is disabled. ElasticBLAST works best with HNS
                enabled.
              </span>
              <button
                onClick={() => {
                  setHnsDismissed(true);
                  try {
                    localStorage.setItem(HNS_DISMISSED_KEY, "1");
                  } catch {
                    /* noop */
                  }
                }}
                style={{
                  background: "none",
                  border: "none",
                  color: "var(--text-faint)",
                  cursor: "pointer",
                  padding: 2,
                }}
                title="Dismiss"
              >
                <X size={12} />
              </button>
            </div>
          )}

          {/* #4: Grid layout for status info */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(100px, 1fr))",
              gap: "var(--space-2)",
              fontSize: 12,
              marginBottom: "var(--space-3)",
            }}
          >
            <div>
              <div className="muted" style={{ fontSize: 10, textTransform: "uppercase" }}>
                Region
              </div>
              <div>{query.data.region}</div>
            </div>
            <div>
              <div className="muted" style={{ fontSize: 10, textTransform: "uppercase" }}>
                SKU
              </div>
              <div>{query.data.sku}</div>
            </div>
            <div>
              <div className="muted" style={{ fontSize: 10, textTransform: "uppercase" }}>
                HNS
              </div>
              {/* #3: HNS neutral color */}
              <div
                style={{
                  color: query.data.is_hns_enabled
                    ? "var(--text-muted)"
                    : "var(--warning)",
                }}
              >
                {query.data.is_hns_enabled ? "Enabled" : "Disabled"}
              </div>
            </div>
            <div>
              <div className="muted" style={{ fontSize: 10, textTransform: "uppercase" }}>
                Public
              </div>
              <div
                style={{
                  color: isPublic ? "var(--warning)" : "var(--success)",
                  fontWeight: 600,
                }}
              >
                {isPublic ? "Enabled" : "Disabled"}
              </div>
            </div>
          </div>

          {/* #16: Compact container table */}
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: 12,
              marginBottom: "var(--space-3)",
            }}
          >
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border-weak)" }}>
                <th
                  style={{
                    textAlign: "left",
                    padding: "4px 0",
                    color: "var(--text-faint)",
                    fontSize: 10,
                    textTransform: "uppercase",
                    fontWeight: 500,
                  }}
                >
                  Container
                </th>
                <th
                  style={{
                    textAlign: "right",
                    padding: "4px 0",
                    color: "var(--text-faint)",
                    fontSize: 10,
                    textTransform: "uppercase",
                    fontWeight: 500,
                  }}
                >
                  Access
                </th>
              </tr>
            </thead>
            <tbody>
              {query.data.containers.map((c) => (
                <tr key={c.name} style={{ borderBottom: "1px solid var(--border-weak)" }}>
                  <td style={{ padding: "6px 0" }}>
                    <strong>{c.name}</strong>
                    {c.last_modified_time && (
                      <span className="muted" style={{ fontSize: 9, marginLeft: 6 }}>
                        updated{" "}
                        {new Date(c.last_modified_time).toLocaleDateString(undefined, {
                          month: "short",
                          day: "numeric",
                        })}
                      </span>
                    )}
                  </td>
                  {/* #1: "None" → "Private" with lock icon */}
                  <td style={{ padding: "6px 0", textAlign: "right" }}>
                    <span
                      style={{
                        fontSize: 10,
                        color: "var(--text-muted)",
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 3,
                      }}
                    >
                      <Lock size={9} /> {c.public_access || "Private"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {/* Toggle status messages */}
          {toggle.isPending && (
            <div
              className="muted"
              style={{
                fontSize: 11,
                color: "var(--accent)",
                marginBottom: "var(--space-2)",
              }}
            >
              <Loader2
                size={10}
                className="spin"
                style={{ display: "inline", verticalAlign: "middle" }}
              />{" "}
              Toggling...
            </div>
          )}
          {toggleMsg && (
            <div
              style={{
                fontSize: 11,
                color: toggleMsg.type === "ok" ? "var(--success)" : "var(--danger)",
                marginBottom: "var(--space-2)",
              }}
            >
              {toggleMsg.type === "ok" ? (
                <CheckCircle2 size={10} style={{ verticalAlign: "middle" }} />
              ) : (
                <AlertTriangle size={10} style={{ verticalAlign: "middle" }} />
              )}{" "}
              {toggleMsg.text}
            </div>
          )}

          {/* #14: Confirmation dialog for enabling public access */}
          {showConfirmEnable && (
            <div
              style={{
                padding: "10px 14px",
                marginBottom: "var(--space-3)",
                background: "rgba(240,198,116,0.08)",
                border: "1px solid rgba(240,198,116,0.25)",
                borderRadius: 8,
                fontSize: 12,
              }}
            >
              <div style={{ color: "var(--warning)", fontWeight: 600, marginBottom: 6 }}>
                <ShieldAlert
                  size={14}
                  style={{ verticalAlign: "middle", marginRight: 4 }}
                />
                Enable public network access?
              </div>
              <div className="muted" style={{ fontSize: 11, marginBottom: 8 }}>
                This allows anyone on the internet to access your storage. ElasticBLAST
                requires this during submit/status/delete operations. Remember to disable
                it after.
              </div>
              <div style={{ display: "flex", gap: "var(--space-2)" }}>
                <button
                  className="glass-button glass-button--primary"
                  onClick={() => {
                    toggle.mutate(true);
                    setShowConfirmEnable(false);
                  }}
                  style={{ fontSize: 11 }}
                >
                  Enable
                </button>
                <button
                  className="glass-button"
                  onClick={() => setShowConfirmEnable(false)}
                  style={{ fontSize: 11 }}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {/* #18: Section header for BLAST DB */}
          <BlastDbSection
            subscriptionId={subscriptionId}
            resourceGroup={resourceGroup}
            accountName={accountName}
            onDownloadingChange={setDbDownloading}
          />
        </>
      )}
    </MonitorCard>
  );
}

// ---------------------------------------------------------------------------
// BLAST Database Section — shows catalog with download status
// ---------------------------------------------------------------------------
function BlastDbSection({
  subscriptionId,
  resourceGroup,
  accountName,
  onDownloadingChange,
}: {
  subscriptionId: string;
  resourceGroup: string;
  accountName: string;
  onDownloadingChange?: (db: string | null) => void;
}) {
  const [downloading, setDownloading] = useState<string | null>(null);
  const [downloadResult, setDownloadResult] = useState<{
    db: string;
    msg: string;
    version?: string;
    type: "ok" | "err";
  } | null>(null);
  // Track in-progress downloads (after API returns, polling continues): { dbName -> { expectedFiles, startTime, version } }
  const [inProgress, setInProgress] = useState<
    Map<string, { expectedFiles: number; startTime: number; sourceVersion?: string }>
  >(new Map());
  // Track locally-completed downloads (survive until refetch succeeds)
  const [locallyDownloaded, setLocallyDownloaded] = useState<
    Map<string, { source_version?: string }>
  >(new Map());

  // Notify parent when downloading state changes (active OR in-progress)
  useEffect(() => {
    const active =
      downloading || (inProgress.size > 0 ? [...inProgress.keys()][0] : null);
    onDownloadingChange?.(active);
  }, [downloading, inProgress, onDownloadingChange]);
  const [customDb, setCustomDb] = useState("");
  const [showCustom, setShowCustom] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [showPopup, setShowPopup] = useState(false);
  const [confirmLargeDb, setConfirmLargeDb] = useState<string | null>(null);

  // ESC key to close popup
  useEffect(() => {
    if (!showPopup) return;
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setShowPopup(false);
        setConfirmLargeDb(null);
      }
    };
    window.addEventListener("keydown", handleEsc);
    return () => window.removeEventListener("keydown", handleEsc);
  }, [showPopup]);

  // Lock body scroll when popup is open
  useEffect(() => {
    if (!showPopup) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [showPopup]);

  // Query downloaded databases from storage
  const dbQuery = useQuery({
    queryKey: ["blast-databases", subscriptionId, accountName, resourceGroup],
    queryFn: () => blastApi.listDatabases(subscriptionId, accountName, resourceGroup),
    enabled: Boolean(subscriptionId && accountName && resourceGroup),
    staleTime: 60_000,
  });

  const publicAccessDisabled = dbQuery.data?.public_access_disabled === true;

  // Check NCBI latest version (lightweight, no storage access needed)
  const latestQuery = useQuery({
    queryKey: ["blast-db-latest-version"],
    queryFn: () => blastApi.checkUpdates(),
    staleTime: 300_000, // 5 min cache
  });
  const latestVersion = latestQuery.data?.latest_version ?? null;

  const downloadedDbs = useMemo(() => {
    const next = new Map<
      string,
      {
        file_count?: number;
        total_bytes?: number;
        last_modified?: string;
        source_version?: string;
        downloaded_at?: string;
      }
    >();
    for (const d of (dbQuery.data?.databases ?? []) as {
      name: string;
      file_count?: number;
      total_bytes?: number;
      last_modified?: string;
      source_version?: string;
      downloaded_at?: string;
    }[]) {
      next.set(d.name, d);
    }
    // Merge locally-completed downloads (show as "Ready" even before refetch succeeds)
    for (const [name, meta] of locallyDownloaded) {
      if (!next.has(name)) {
        next.set(name, {
          source_version: meta.source_version,
          downloaded_at: new Date().toISOString(),
        });
      }
    }
    return next;
  }, [dbQuery.data?.databases, locallyDownloaded]);

  const updatesAvailable = latestVersion
    ? [...downloadedDbs.values()].filter(
        (d) => d.source_version && d.source_version !== latestVersion,
      ).length
    : 0;

  // Track elapsed time during download
  useEffect(() => {
    if (!downloading) {
      setElapsed(0);
      return;
    }
    const start = Date.now();
    const t = setInterval(
      () => setElapsed(Math.floor((Date.now() - start) / 1000)),
      1000,
    );
    return () => clearInterval(t);
  }, [downloading]);

  // Poll database list while there are in-progress downloads (every 10s)
  useEffect(() => {
    if (inProgress.size === 0) return;
    const t = setInterval(() => dbQuery.refetch(), 10_000);
    return () => clearInterval(t);
  }, [inProgress.size, dbQuery]);

  // Mark downloads as complete when actual file count reaches expected
  useEffect(() => {
    if (inProgress.size === 0) return;
    setInProgress((prev) => {
      let changed = false;
      const next = new Map(prev);
      for (const [name, info] of prev) {
        const actual = downloadedDbs.get(name);
        if (actual?.file_count && actual.file_count >= info.expectedFiles * 0.9) {
          // 90% threshold for "complete" (some files may be in pending state)
          next.delete(name);
          changed = true;
          setLocallyDownloaded((p) =>
            new Map(p).set(name, { source_version: info.sourceVersion }),
          );
        }
      }
      return changed ? next : prev;
    });
  }, [downloadedDbs, inProgress]);

  const handleDownload = async (dbName: string) => {
    setDownloading(dbName);
    setDownloadResult(null);
    const startTime = Date.now();
    try {
      const resp = await monitoringApi.prepareBlastDb(
        subscriptionId,
        resourceGroup,
        accountName,
        dbName,
      );
      const total =
        resp.files_total ?? (resp.files_copied ?? 0) + (resp.files_already_copying ?? 0);
      // Track as in-progress (polling will mark as Ready when complete)
      setInProgress((prev) => {
        const next = new Map(prev);
        next.set(dbName, {
          expectedFiles: total,
          startTime,
          sourceVersion: resp.source_version,
        });
        return next;
      });
      setDownloadResult({
        db: dbName,
        msg: resp.async
          ? `Started copying ${total} files in background. Status will update as files arrive.`
          : `${resp.files_copied ?? 0} files started${resp.files_already_copying ? `, ${resp.files_already_copying} already in progress` : ""}`,
        version: resp.source_version,
        type: "ok",
      });
      // Refresh database list
      dbQuery.refetch();
    } catch (e) {
      setDownloadResult({ db: dbName, msg: formatApiError(e, "storage"), type: "err" });
    } finally {
      setDownloading(null);
    }
  };

  // Group catalog by category
  const categories = ["Small / Test", "Medium", "Large"] as const;

  return (
    <div
      style={{
        marginTop: "var(--space-3)",
        paddingTop: "var(--space-3)",
        borderTop: "1px solid var(--border-weak)",
      }}
    >
      {/* Header — always visible */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          width: "100%",
          padding: 0,
        }}
      >
        <h4
          style={{
            margin: 0,
            fontSize: 13,
            fontWeight: 600,
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          <Database size={14} strokeWidth={1.5} /> BLAST Databases
        </h4>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {dbQuery.isLoading && (
            <Loader2 size={12} className="spin" style={{ color: "var(--text-faint)" }} />
          )}
          {downloadedDbs.size > 0 && (
            <span className="gt gt-g" style={{ fontSize: 9 }}>
              {downloadedDbs.size} ready
            </span>
          )}
          {updatesAvailable > 0 && (
            <span className="gt gt-o" style={{ fontSize: 9 }}>
              {updatesAvailable} update{updatesAvailable > 1 ? "s" : ""}
            </span>
          )}
          <span style={{ fontSize: 10, color: "var(--text-faint)" }}>
            {downloadedDbs.size}/{DB_CATALOG.length}
          </span>
          <button
            className="glass-button"
            style={{ padding: "3px 6px", border: "none" }}
            onClick={() => {
              setShowPopup(true);
              dbQuery.refetch();
            }}
            title="Open database manager"
          >
            <Maximize2 size={12} strokeWidth={1.5} />
          </button>
        </div>
      </div>

      {/* Inline summary — show downloaded DB names */}
      {downloadedDbs.size > 0 && (
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginTop: 6 }}>
          {[...downloadedDbs.keys()].map((name) => (
            <span key={name} className="gt gt-g" style={{ fontSize: 9 }}>
              {name}
            </span>
          ))}
        </div>
      )}

      {/* Popup modal for full database list */}
      {showPopup &&
        createPortal(
          <div
            className="glass-dialog-backdrop"
            onClick={(e) => {
              if (e.target === e.currentTarget) setShowPopup(false);
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
                    onClick={() => {
                      setShowPopup(false);
                      setConfirmLargeDb(null);
                    }}
                    style={{ padding: "4px 6px", border: "none" }}
                    title="Close"
                  >
                    <X size={16} strokeWidth={1.5} />
                  </button>
                </div>
              </div>
              {/* Summary stats */}
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
                <span>
                  {downloadedDbs.size}/{DB_CATALOG.length} downloaded
                </span>
                {downloadedDbs.size > 0 && (
                  <span>
                    {formatBytes(
                      [...downloadedDbs.values()].reduce(
                        (s, d) => s + (d.total_bytes ?? 0),
                        0,
                      ),
                    )}{" "}
                    used
                  </span>
                )}
                {updatesAvailable > 0 && (
                  <span style={{ color: "var(--warning)", fontWeight: 600 }}>
                    {updatesAvailable} update{updatesAvailable > 1 ? "s" : ""} available
                  </span>
                )}
                {latestVersion && (
                  <span>
                    NCBI latest:{" "}
                    <code style={{ fontSize: 9 }}>
                      {formatNcbiVersion(latestVersion)}
                    </code>
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
              {/* Download result toast — shown at top, fades out after 5s */}
              {downloadResult && (
                <StorageDownloadResultBanner
                  result={downloadResult}
                  onDismiss={() => setDownloadResult(null)}
                />
              )}
              {/* Public access disabled warning */}
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
                    gap: 8,
                  }}
                >
                  <Lock size={14} style={{ flexShrink: 0, marginTop: 1 }} />
                  <div>
                    <strong>Storage public access is disabled.</strong> Database scan is
                    unavailable. Enable public access on the Storage card (Unlock button)
                    to see which databases are downloaded. The catalog below still shows
                    all available databases.
                  </div>
                </div>
              )}
              <div style={{ overflowY: "auto", flex: 1, paddingRight: 4 }}>
                {/* ── Full database list ── */}
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {categories.map((cat) => {
                    const dbs = DB_CATALOG.filter((d) => d.category === cat);
                    return (
                      <div key={cat}>
                        <div
                          style={{
                            fontSize: 9,
                            color: "var(--text-faint)",
                            textTransform: "uppercase",
                            letterSpacing: "0.06em",
                            padding: "4px 0 2px",
                            borderBottom: "1px solid var(--border-weak)",
                            marginBottom: 4,
                          }}
                        >
                          {cat}
                          {cat === "Large" && (
                            <span
                              style={{
                                marginLeft: 6,
                                fontSize: 8,
                                color: "var(--warning)",
                                textTransform: "none",
                                letterSpacing: 0,
                              }}
                            >
                              ⚠ Large downloads may take hours
                            </span>
                          )}
                        </div>
                        {dbs.map((db) => {
                          const inProgressInfo = inProgress.get(db.value);
                          const isCopying = Boolean(inProgressInfo);
                          // While copying, don't show as downloaded — show progress
                          const isDownloaded = !isCopying && downloadedDbs.has(db.value);
                          const isDownloading = downloading === db.value;
                          const meta = downloadedDbs.get(db.value);
                          const copyProgress = inProgressInfo
                            ? Math.min(
                                100,
                                Math.round(
                                  ((meta?.file_count ?? 0) /
                                    inProgressInfo.expectedFiles) *
                                    100,
                                ),
                              )
                            : 0;
                          return (
                            <div
                              key={db.value}
                              style={{
                                display: "flex",
                                alignItems: "center",
                                gap: 8,
                                padding: "6px 8px",
                                borderRadius: 6,
                                background: isDownloaded
                                  ? "rgba(115,191,105,0.04)"
                                  : "transparent",
                                border: `1px solid ${isDownloaded ? "rgba(115,191,105,0.1)" : "var(--border-weak)"}`,
                                marginBottom: 3,
                              }}
                            >
                              {/* Status icon */}
                              <div style={{ flexShrink: 0 }}>
                                {isDownloaded ? (
                                  <CheckCircle2
                                    size={14}
                                    style={{ color: "var(--success)" }}
                                  />
                                ) : isDownloading ? (
                                  <Loader2
                                    size={14}
                                    className="spin"
                                    style={{ color: "var(--accent)" }}
                                  />
                                ) : (
                                  <Circle
                                    size={14}
                                    style={{ color: "var(--text-faint)", opacity: 0.3 }}
                                  />
                                )}
                              </div>
                              {/* Info — stacked vertically */}
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <div
                                  style={{
                                    display: "flex",
                                    alignItems: "center",
                                    gap: 6,
                                    flexWrap: "wrap",
                                  }}
                                >
                                  <span
                                    style={{
                                      fontSize: 12,
                                      fontWeight: isDownloaded ? 600 : 400,
                                    }}
                                  >
                                    {db.label}
                                  </span>
                                  <span
                                    className={`gt ${db.type === "nucl" ? "gt-b" : "gt-p"}`}
                                    style={{ fontSize: 8 }}
                                  >
                                    {db.type === "nucl" ? "N" : "P"}
                                  </span>
                                  <span
                                    style={{ fontSize: 10, color: "var(--text-faint)" }}
                                  >
                                    {db.size}
                                  </span>
                                  <code
                                    style={{
                                      fontSize: 9,
                                      color: "var(--text-faint)",
                                      background: "var(--bg-tertiary)",
                                      padding: "1px 4px",
                                      borderRadius: 3,
                                    }}
                                  >
                                    {db.value}
                                  </code>
                                </div>
                                <div
                                  style={{
                                    fontSize: 10,
                                    color: "var(--text-muted)",
                                    marginTop: 2,
                                  }}
                                >
                                  {db.desc}
                                </div>
                                {/* Download estimate for not-yet-downloaded DBs */}
                                {!isDownloaded && !isDownloading && (
                                  <div
                                    style={{
                                      fontSize: 9,
                                      color: "var(--text-faint)",
                                      marginTop: 2,
                                    }}
                                  >
                                    Est. {db.estFiles} files · {db.estMinutes}
                                  </div>
                                )}
                                {/* Downloading progress */}
                                {isDownloading && (
                                  <div
                                    style={{
                                      fontSize: 10,
                                      color: "var(--accent)",
                                      marginTop: 2,
                                      display: "flex",
                                      alignItems: "center",
                                      gap: 6,
                                    }}
                                  >
                                    <span>Initiating copy...</span>
                                    <span style={{ fontFamily: "var(--font-mono)" }}>
                                      {elapsed}s
                                    </span>
                                  </div>
                                )}
                                {/* In-progress copy (after API returned) */}
                                {isCopying && inProgressInfo && (
                                  <div style={{ marginTop: 4 }}>
                                    <div
                                      style={{
                                        fontSize: 10,
                                        color: "var(--accent)",
                                        display: "flex",
                                        alignItems: "center",
                                        gap: 6,
                                        marginBottom: 3,
                                      }}
                                    >
                                      <Loader2 size={10} className="spin" />
                                      <span>
                                        Copying {meta?.file_count ?? 0} /{" "}
                                        {inProgressInfo.expectedFiles} files
                                      </span>
                                      <span
                                        style={{
                                          fontFamily: "var(--font-mono)",
                                          color: "var(--text-faint)",
                                        }}
                                      >
                                        ·{" "}
                                        {Math.floor(
                                          (Date.now() - inProgressInfo.startTime) / 1000,
                                        )}
                                        s
                                      </span>
                                      {db.estMinutes && (
                                        <span
                                          style={{
                                            color: "var(--text-faint)",
                                            fontSize: 9,
                                          }}
                                        >
                                          · est. {db.estMinutes}
                                        </span>
                                      )}
                                    </div>
                                    {/* Progress bar */}
                                    <div
                                      style={{
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
                                          borderRadius: 2,
                                          transition: "width 0.5s ease",
                                        }}
                                      />
                                    </div>
                                  </div>
                                )}
                                {/* Downloaded metadata: actual size, file count, date, version */}
                                {isDownloaded && meta && (
                                  <div
                                    style={{
                                      fontSize: 10,
                                      color: "var(--text-muted)",
                                      marginTop: 2,
                                      display: "flex",
                                      gap: 8,
                                      flexWrap: "wrap",
                                      alignItems: "center",
                                    }}
                                  >
                                    {meta.total_bytes ? (
                                      <span style={{ color: "var(--success)" }}>
                                        {formatBytes(meta.total_bytes)}
                                      </span>
                                    ) : null}
                                    {meta.file_count ? (
                                      <span style={{ color: "var(--success)" }}>
                                        {meta.file_count} files
                                      </span>
                                    ) : null}
                                    {meta.last_modified ? (
                                      <span>{formatStorageDate(meta.last_modified)}</span>
                                    ) : null}
                                    {meta.source_version && (
                                      <code
                                        style={{
                                          fontSize: 9,
                                          background: "var(--bg-tertiary)",
                                          padding: "1px 4px",
                                          borderRadius: 3,
                                        }}
                                      >
                                        v:{formatNcbiVersion(meta.source_version)}
                                      </code>
                                    )}
                                    {meta.source_version &&
                                      latestVersion &&
                                      meta.source_version !== latestVersion && (
                                        <span
                                          style={{
                                            color: "var(--warning)",
                                            fontWeight: 600,
                                            fontSize: 9,
                                          }}
                                        >
                                          Update available
                                        </span>
                                      )}
                                  </div>
                                )}
                              </div>
                              {/* Action */}
                              <div
                                style={{
                                  flexShrink: 0,
                                  display: "flex",
                                  flexDirection: "column",
                                  alignItems: "flex-end",
                                  gap: 3,
                                }}
                              >
                                {isDownloaded ? (
                                  <>
                                    {meta?.source_version &&
                                    latestVersion &&
                                    meta.source_version !== latestVersion ? (
                                      <button
                                        className="glass-button"
                                        style={{
                                          fontSize: 10,
                                          padding: "2px 8px",
                                          color: "var(--warning)",
                                          borderColor: "rgba(240,198,116,0.3)",
                                        }}
                                        onClick={(e) => {
                                          e.stopPropagation();
                                          if (db.category === "Large") {
                                            setConfirmLargeDb(db.value);
                                          } else {
                                            handleDownload(db.value);
                                          }
                                        }}
                                        disabled={downloading !== null}
                                        title={`Update from ${formatNcbiVersion(meta.source_version)} to ${formatNcbiVersion(latestVersion)}`}
                                      >
                                        <Download size={10} /> Update
                                      </button>
                                    ) : (
                                      <span
                                        style={{
                                          fontSize: 10,
                                          color: "var(--success)",
                                          fontWeight: 500,
                                        }}
                                      >
                                        Ready
                                      </span>
                                    )}
                                  </>
                                ) : isDownloading ? (
                                  <span style={{ fontSize: 10, color: "var(--accent)" }}>
                                    {elapsed}s
                                  </span>
                                ) : isCopying ? (
                                  <span
                                    style={{
                                      fontSize: 10,
                                      color: "var(--accent)",
                                      display: "flex",
                                      alignItems: "center",
                                      gap: 4,
                                    }}
                                  >
                                    <Loader2 size={10} className="spin" /> {copyProgress}%
                                  </span>
                                ) : (
                                  <button
                                    className="glass-button glass-button--primary"
                                    style={{ fontSize: 10, padding: "2px 8px" }}
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      if (db.category === "Large") {
                                        setConfirmLargeDb(db.value);
                                      } else {
                                        handleDownload(db.value);
                                      }
                                    }}
                                    disabled={downloading !== null}
                                    title={
                                      downloading !== null
                                        ? "Another download is in progress"
                                        : `Download ${db.value}`
                                    }
                                  >
                                    <Download size={10} /> Get
                                  </button>
                                )}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    );
                  })}
                </div>

                {/* Custom DB name */}
                <div style={{ marginTop: "var(--space-2)" }}>
                  {!showCustom ? (
                    <button
                      className="glass-button"
                      style={{ fontSize: 10, padding: "2px 8px" }}
                      onClick={() => setShowCustom(true)}
                    >
                      + Custom database
                    </button>
                  ) : (
                    <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                      <input
                        className="glass-input"
                        value={customDb}
                        onChange={(e) => setCustomDb(e.target.value)}
                        placeholder="e.g. refseq_rna"
                        style={{ width: 160, fontSize: 11, padding: "4px 8px" }}
                        spellCheck={false}
                      />
                      <button
                        className="glass-button glass-button--primary"
                        style={{ fontSize: 10, padding: "2px 8px" }}
                        onClick={() => {
                          if (customDb) handleDownload(customDb);
                        }}
                        disabled={!customDb || downloading !== null}
                      >
                        <Download size={10} /> Get
                      </button>
                      <button
                        className="glass-button"
                        style={{ fontSize: 10, padding: "2px 8px" }}
                        onClick={() => {
                          setShowCustom(false);
                          setCustomDb("");
                        }}
                      >
                        Cancel
                      </button>
                    </div>
                  )}
                </div>

                {/* Large DB download confirmation */}
                {confirmLargeDb &&
                  (() => {
                    const db = DB_CATALOG.find((d) => d.value === confirmLargeDb);
                    return (
                      <div
                        style={{
                          marginTop: "var(--space-2)",
                          padding: "10px 14px",
                          borderRadius: 8,
                          fontSize: 12,
                          background: "rgba(240,198,116,0.08)",
                          border: "1px solid rgba(240,198,116,0.25)",
                        }}
                      >
                        <div
                          style={{
                            color: "var(--warning)",
                            fontWeight: 600,
                            marginBottom: 6,
                            display: "flex",
                            alignItems: "center",
                            gap: 6,
                          }}
                        >
                          <AlertTriangle size={14} /> Download{" "}
                          {db?.label ?? confirmLargeDb}?
                        </div>
                        <div className="muted" style={{ fontSize: 11, marginBottom: 8 }}>
                          This database is <strong>{db?.size}</strong> and may take hours
                          to copy from NCBI. Ensure your storage account has sufficient
                          space and that public access is enabled.
                        </div>
                        <div style={{ display: "flex", gap: "var(--space-2)" }}>
                          <button
                            className="glass-button glass-button--primary"
                            onClick={() => {
                              handleDownload(confirmLargeDb);
                              setConfirmLargeDb(null);
                            }}
                            style={{ fontSize: 11 }}
                          >
                            <Download size={10} /> Start Download
                          </button>
                          <button
                            className="glass-button"
                            onClick={() => setConfirmLargeDb(null)}
                            style={{ fontSize: 11 }}
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    );
                  })()}

                {/* Download result message */}

                {/* Info footer */}
                <div
                  className="muted"
                  style={{ fontSize: 10, marginTop: "var(--space-2)" }}
                >
                  Server-side copy from NCBI S3 →{" "}
                  <code style={{ fontSize: 10 }}>blast-db</code> container. No local
                  download required.
                </div>
              </div>
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}
