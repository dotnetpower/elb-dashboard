import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { formatApiError } from "@/api/client";
import { blastApi, monitoringApi, storageApi } from "@/api/endpoints";
import type { StorageLocalDebugStatus } from "@/api/storage";

/**
 * Single source of truth for "what BLAST databases exist in this storage account
 * and what the user is currently doing with them" — used by `BlastDbSection` and
 * the modal it opens.
 *
 * Lifecycles tracked:
 *   - `downloading`     : copy request is in-flight (button click → API ack)
 *   - `inProgress`      : copy was acknowledged; we poll until file_count >= 90% expected
 *   - `locallyDownloaded`: copy completed locally; survives until refetch confirms
 *
 * The `downloadedDbs` map merges what storage actually reports with the "locally
 * just finished" set so the UI flips from spinner → Ready instantly.
 */

export interface DownloadResult {
  db: string;
  msg: string;
  version?: string;
  type: "ok" | "err";
}

interface InProgressInfo {
  expectedFiles: number;
  startTime: number;
  sourceVersion?: string;
}

export interface DownloadedDbMeta {
  file_count?: number;
  total_bytes?: number;
  last_modified?: string;
  source_version?: string;
  signature_etag?: string;
  downloaded_at?: string;
  /** True when prepare-db has uploaded preset shard layouts for this DB. */
  sharded?: boolean;
  /** Sorted preset shard counts already built (e.g. [1,2,3,4,5,6,8,10]). */
  shard_sets?: number[];
  shard_source_version?: string | null;
  shards_stale?: boolean;
  update_in_progress?: boolean;
  updating_to_source_version?: string | null;
  update_started_at?: string | null;
  update_completed_at?: string | null;
  update_error?: string | null;
  update_failed_at?: string | null;
  /**
   * Honest per-DB copy lifecycle from the hardened prepare-db pipeline.
   * Source of truth for "is this DB actually downloaded": phase === "completed"
   * is the ONLY value that means Ready. The pre-hardening SPA used
   * `file_count >= 90% of expected` which surfaced partial copies as Ready.
   */
  copy_status?: {
    phase: "copying" | "partial" | "init_failed" | "completed" | string;
    total_files?: number;
    success?: number;
    failed?: number;
    aborted?: number;
    pending?: number;
    timed_out?: boolean;
  };
  failed_files?: Array<{ blob: string; status: string; reason?: string }>;
  db_order_oracle?: {
    status: string;
    run_id?: string | null;
    started_at?: string | null;
    source_version?: string | null;
    expected_parts?: number;
    ready_parts?: number;
    part_prefix?: string | null;
  };
}

interface UseBlastDbArgs {
  subscriptionId: string;
  resourceGroup: string;
  accountName: string;
  clusterName: string;
  acrName?: string;
  enabled: boolean;
}

export function useBlastDb({
  subscriptionId,
  resourceGroup,
  accountName,
  clusterName,
  acrName,
  enabled,
}: UseBlastDbArgs) {
  const [downloading, setDownloading] = useState<string | null>(null);
  const [downloadResult, setDownloadResult] = useState<DownloadResult | null>(null);
  const [inProgress, setInProgress] = useState<Map<string, InProgressInfo>>(
    () => new Map(),
  );
  const [locallyDownloaded, setLocallyDownloaded] = useState<
    Map<string, { source_version?: string }>
  >(() => new Map());
  const [elapsed, setElapsed] = useState(0);
  const [oracleBuilding, setOracleBuilding] = useState<string | null>(null);

  const dbQuery = useQuery({
    queryKey: ["blast-databases", subscriptionId, accountName, resourceGroup],
    queryFn: () => blastApi.listDatabases(subscriptionId, accountName, resourceGroup),
    enabled,
    staleTime: 60_000,
  });

  const latestQuery = useQuery({
    queryKey: [
      "blast-db-latest-version",
      subscriptionId,
      accountName,
      resourceGroup,
    ],
    queryFn: () =>
      blastApi.checkUpdates(
        subscriptionId && accountName && resourceGroup
          ? {
              subscriptionId,
              storageAccount: accountName,
              resourceGroup,
            }
          : undefined,
      ),
    staleTime: 300_000,
  });
  const latestVersion = latestQuery.data?.latest_version ?? null;
  const updatesAvailableByDb = useMemo(() => {
    const map = new Map<string, { snapshot?: string; etag?: string }>();
    for (const item of latestQuery.data?.updates_available ?? []) {
      if (item?.db) {
        map.set(item.db, {
          snapshot: item.snapshot,
          etag: item.signature_etag,
        });
      }
    }
    return map;
  }, [latestQuery.data?.updates_available]);

  // The backend signals "local laptop cannot reach data plane" through either
  // private-only accounts or selected-network firewall rejects. Treat both as
  // local-debug recoverable so the IP allowlist action remains visible.
  const dbDegradedReason = (dbQuery.data as { degraded_reason?: string } | undefined)
    ?.degraded_reason;
  const localFirewallBlocked = dbDegradedReason === "firewall_blocked";
  const publicAccessDisabled =
    dbQuery.data?.public_access_disabled === true ||
    dbDegradedReason === "network_blocked" ||
    localFirewallBlocked;
  const storageAccessTitle = localFirewallBlocked
    ? "Storage firewall is still blocking this host"
    : "Storage is Private only";
  const storageAccessHint = localFirewallBlocked
    ? "This local browser session is still being rejected by the selected-network firewall. Refresh the IP allowlist for local testing; production continues to use the private endpoint."
    : "This local browser session cannot read the database list through the private endpoint. Open an IP-allowlisted debug window for local testing; production continues to use the private endpoint.";

  // Local-debug toggle visibility — fetched once, refreshed every 60s while
  // the storage account is blocked. The endpoint always returns 200 with
  // is_local=false in deployed environments, so the button never leaks into
  // production via this query.
  const localDebugQuery = useQuery({
    queryKey: ["storage-local-debug", subscriptionId, resourceGroup, accountName],
    queryFn: () =>
      storageApi.localDebugStatus(subscriptionId, resourceGroup, accountName),
    enabled,
    staleTime: 30_000,
    refetchInterval: publicAccessDisabled ? 30_000 : 5 * 60_000,
  });
  const localDebug: StorageLocalDebugStatus | undefined = localDebugQuery.data;
  const canEnableLocalAccess = localDebug?.is_local === true;

  const downloadedDbs = useMemo(() => {
    const next = new Map<string, DownloadedDbMeta>();
    for (const d of (dbQuery.data?.databases ?? []) as (DownloadedDbMeta & {
      name: string;
    })[]) {
      next.set(d.name, d);
    }
    for (const [name, meta] of locallyDownloaded) {
      if (!next.has(name)) {
        next.set(name, {
          source_version: meta.source_version,
          downloaded_at: new Date().toISOString(),
          copy_status: { phase: "completed" },
        });
      }
    }
    return next;
  }, [dbQuery.data?.databases, locallyDownloaded]);

  /**
   * Authoritative "this DB is genuinely usable for BLAST" predicate.
   * Modern entries (post-hardening) carry ``copy_status.phase`` — only
   * ``completed`` counts. Legacy entries without ``copy_status`` fall back
   * to "has files and is not mid-update", which preserves the behaviour
   * for DBs prepared before the hardening shipped.
   */
  const isDbReady = (meta: DownloadedDbMeta | undefined): boolean => {
    if (!meta) return false;
    if (meta.copy_status?.phase) {
      return meta.copy_status.phase === "completed";
    }
    if (meta.update_in_progress) return false;
    return Boolean(meta.file_count && meta.file_count > 0);
  };

  const updatesAvailable = useMemo(() => {
    // Prefer the server-side per-DB list (compares NCBI ETag against the
    // ETag stored when prepare-db landed) — it does not fire whenever
    // ``latest-dir`` rotates. Fall back to the legacy heuristic only when
    // the server omitted the per-DB list (no storage scope passed).
    if (updatesAvailableByDb.size > 0) {
      let n = 0;
      for (const [name] of updatesAvailableByDb) {
        const meta = downloadedDbs.get(name);
        if (meta && !meta.update_in_progress) n += 1;
      }
      return n;
    }
    if (!latestVersion) return 0;
    return [...downloadedDbs.values()].filter(
      (d) =>
        d.source_version && d.source_version !== latestVersion && !d.update_in_progress,
    ).length;
  }, [downloadedDbs, latestVersion, updatesAvailableByDb]);

  // Tick elapsed seconds while ANY copy is in-flight — either the API ack
  // is pending (``downloading``) or the server-side copy daemon is polling
  // (``inProgress`` map populated). Pre-hardening this was tied to
  // ``downloading`` alone, so the elapsed counter reset to 0 immediately
  // after the POST returned and the user saw "0s" while the multi-hour
  // server-side copy was still running.
  useEffect(() => {
    const anyActive = Boolean(downloading) || inProgress.size > 0;
    if (!anyActive) {
      setElapsed(0);
      return;
    }
    const earliestStart = (() => {
      let earliest = Date.now();
      for (const info of inProgress.values()) {
        if (info.startTime < earliest) earliest = info.startTime;
      }
      return earliest;
    })();
    setElapsed(Math.floor((Date.now() - earliestStart) / 1000));
    const t = setInterval(
      () => setElapsed(Math.floor((Date.now() - earliestStart) / 1000)),
      1000,
    );
    return () => clearInterval(t);
  }, [downloading, inProgress]);

  // Poll storage every 10s while any copy is in progress.
  // Depends only on `dbQuery.refetch` (stable across renders in react-query),
  // not on the whole `dbQuery` object — otherwise the interval would be torn
  // down and recreated on every parent render.
  const refetchDbList = dbQuery.refetch;
  useEffect(() => {
    if (inProgress.size === 0) return;
    const t = setInterval(() => {
      void refetchDbList();
    }, 10_000);
    return () => clearInterval(t);
  }, [inProgress.size, refetchDbList]);

  // Detect copy completion honestly — the hardened prepare-db pipeline writes
  // ``copy_status.phase`` into ``{db}-metadata.json``. Only ``completed`` is
  // success; ``partial`` / ``init_failed`` mean the user must intervene
  // (retry / check NCBI). The pre-hardening 90%-of-files heuristic would
  // mark partial successes as "Ready" forever, which broke later BLAST
  // submits with cryptic "DB not found" errors.
  useEffect(() => {
    if (inProgress.size === 0) return;
    setInProgress((prev) => {
      let changed = false;
      const next = new Map(prev);
      for (const [name, info] of prev) {
        const actual = downloadedDbs.get(name);
        const phase = actual?.copy_status?.phase;
        if (
          phase === "completed" ||
          phase === "partial" ||
          phase === "init_failed"
        ) {
          next.delete(name);
          changed = true;
          if (phase === "completed") {
            setLocallyDownloaded((p) =>
              new Map(p).set(name, { source_version: info.sourceVersion }),
            );
          } else {
            // Partial/init-failed: keep the user informed with an error toast
            // instead of silently flipping to "Ready".
            const fileCount = actual?.copy_status?.success ?? 0;
            const total = actual?.copy_status?.total_files ?? info.expectedFiles;
            const failed =
              (actual?.copy_status?.failed ?? 0) +
              (actual?.copy_status?.aborted ?? 0);
            setDownloadResult({
              db: name,
              msg:
                actual?.update_error ||
                `Server-side copy partial: ${fileCount}/${total} succeeded, ${failed} failed. Retry from the catalog.`,
              type: "err",
            });
          }
        }
      }
      return changed ? next : prev;
    });
  }, [downloadedDbs, inProgress]);

  const handleDownload = async (
    dbName: string,
    mode: "download" | "update" = "download",
  ) => {
    if (!enabled) return;
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
          ? mode === "update"
            ? `Started DB update copy for ${total} files. Existing generation remains active until metadata promotion.`
            : `Started copying ${total} files in background. Status will update as files arrive.`
          : `${resp.files_copied ?? 0} files started${
              resp.files_already_copying
                ? `, ${resp.files_already_copying} already in progress`
                : ""
            }`,
        version: resp.source_version,
        type: "ok",
      });
      void dbQuery.refetch();
    } catch (e) {
      setDownloadResult({ db: dbName, msg: formatApiError(e, "storage"), type: "err" });
    } finally {
      setDownloading(null);
    }
  };

  const handleUpdate = (dbName: string) => handleDownload(dbName, "update");

  const handleBuildOracle = async (dbName: string) => {
    if (!enabled || !clusterName) return;
    setOracleBuilding(dbName);
    setDownloadResult(null);
    try {
      const meta = downloadedDbs.get(dbName);
      const resp = await blastApi.buildDbOrderOracle(
        {
          subscription_id: subscriptionId,
          resource_group: resourceGroup,
          account_name: accountName,
          cluster_name: clusterName,
          acr_name: acrName,
          source_version: meta?.source_version,
        },
        dbName,
      );
      setDownloadResult({
        db: dbName,
        msg: `Started order oracle build across ${resp.expected_parts} warmed shards.`,
        type: "ok",
      });
      void dbQuery.refetch();
    } catch (e) {
      setDownloadResult({ db: dbName, msg: formatApiError(e, "blast"), type: "err" });
    } finally {
      setOracleBuilding(null);
    }
  };

  // Aggregate "is anything happening" — used by parent for shimmer
  const activeDownload =
    downloading ?? (inProgress.size > 0 ? [...inProgress.keys()][0] : null);

  // Trigger an explicit local-debug open and refetch the DB list on success.
  const [openingLocalDebug, setOpeningLocalDebug] = useState(false);
  const [localDebugError, setLocalDebugError] = useState<string | null>(null);
  const enableLocalAccess = async (): Promise<{
    ok: boolean;
    message: string;
  }> => {
    if (!enabled) return { ok: false, message: "select a storage account first" };
    setOpeningLocalDebug(true);
    setLocalDebugError(null);
    try {
      const result = await storageApi.localDebugOpen(
        subscriptionId,
        resourceGroup,
        accountName,
      );
      void dbQuery.refetch();
      void localDebugQuery.refetch();
      const summary =
        result.action === "already_open"
          ? `Already open to ${result.ip ?? "this IP"}`
          : result.action === "ip_added"
            ? `Added ${result.ip ?? "this IP"} to allowlist`
            : result.action === "opened"
              ? `Opened (was publicNetworkAccess=${result.previous_public ?? "Disabled"}) to ${result.ip ?? "this IP"}`
              : result.action === "noop"
                ? `No-op: ${result.reason ?? "refused"}`
                : `Failed: ${result.error ?? "unknown error"}`;
      return {
        ok: result.action !== "failed" && result.action !== "noop",
        message: summary,
      };
    } catch (e) {
      const msg = formatApiError(e, "storage");
      setLocalDebugError(msg);
      return { ok: false, message: msg };
    } finally {
      setOpeningLocalDebug(false);
    }
  };

  return {
    // Query state
    dbQuery,
    latestVersion,
    publicAccessDisabled,
    localFirewallBlocked,
    storageAccessTitle,
    storageAccessHint,
    downloadedDbs,
    isDbReady,
    updatesAvailable,
    updatesAvailableByDb,
    // Local-debug state (only meaningful when the api process is local)
    canEnableLocalAccess,
    localDebug,
    openingLocalDebug,
    localDebugError,
    enableLocalAccess,
    // In-flight state
    downloading,
    oracleBuilding,
    elapsed,
    inProgress,
    activeDownload,
    // Result toast
    downloadResult,
    dismissDownloadResult: () => setDownloadResult(null),
    // Actions
    handleDownload,
    handleUpdate,
    handleBuildOracle,
  };
}

export type UseBlastDbReturn = ReturnType<typeof useBlastDb>;
