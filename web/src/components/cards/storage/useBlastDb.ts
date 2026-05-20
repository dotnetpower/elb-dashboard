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
    queryKey: ["blast-db-latest-version"],
    queryFn: () => blastApi.checkUpdates(),
    staleTime: 300_000,
  });
  const latestVersion = latestQuery.data?.latest_version ?? null;

  // The backend signals "local laptop cannot reach data plane" two ways:
  //  - explicit `public_access_disabled: true` flag, OR
  //  - `degraded_reason === "network_blocked"` (older code path).
  // Treat both identically so the UI's blocked-state UX always lights up.
  const dbDegradedReason = (dbQuery.data as { degraded_reason?: string } | undefined)
    ?.degraded_reason;
  const publicAccessDisabled =
    dbQuery.data?.public_access_disabled === true ||
    dbDegradedReason === "network_blocked";

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
        });
      }
    }
    return next;
  }, [dbQuery.data?.databases, locallyDownloaded]);

  const updatesAvailable = useMemo(() => {
    if (!latestVersion) return 0;
    return [...downloadedDbs.values()].filter(
      (d) =>
        d.source_version &&
        d.source_version !== latestVersion &&
        !d.update_in_progress,
    ).length;
  }, [downloadedDbs, latestVersion]);

  // Tick elapsed seconds while a download API call is in-flight
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

  // Detect copy completion (>=90% of expected files materialised)
  useEffect(() => {
    if (inProgress.size === 0) return;
    setInProgress((prev) => {
      let changed = false;
      const next = new Map(prev);
      for (const [name, info] of prev) {
        const actual = downloadedDbs.get(name);
        if (actual?.file_count && actual.file_count >= info.expectedFiles * 0.9) {
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

  const handleDownload = async (dbName: string, mode: "download" | "update" = "download") => {
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
      return { ok: result.action !== "failed" && result.action !== "noop", message: summary };
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
    downloadedDbs,
    updatesAvailable,
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
