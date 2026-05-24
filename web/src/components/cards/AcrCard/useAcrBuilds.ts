import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { formatApiError } from "@/api/client";
import { monitoringApi } from "@/api/endpoints";
import { useAutoRefreshInterval } from "@/hooks/useAutoRefresh";

export type BuildStatus = "idle" | "queued" | "building" | "done" | "error";
const BUILD_QUEUE_TIMEOUT_SECONDS = 5 * 60;

export interface BuildResult {
  image: string;
  status: string;
  error?: string;
  run_id?: string;
  acr_status?: string;
}

export interface UseAcrBuildsArgs {
  subscriptionId: string;
  resourceGroup: string;
  registryName: string;
}

export function useAcrBuilds({
  subscriptionId,
  resourceGroup,
  registryName,
}: UseAcrBuildsArgs) {
  const enabled = Boolean(subscriptionId && resourceGroup && registryName);
  const [buildStatus, setBuildStatus] = useState<BuildStatus>("idle");
  const [showConfirm, setShowConfirm] = useState(false);
  const [expandedError, setExpandedError] = useState<string | null>(null);
  const idleRefetchInterval = useAutoRefreshInterval();

  const query = useQuery({
    queryKey: ["acr", subscriptionId, resourceGroup, registryName],
    queryFn: () => monitoringApi.acr(subscriptionId, resourceGroup, registryName),
    enabled,
    refetchInterval: (q) => {
      const data = q.state.data;
      // Keep a fast 10s poll while a build is active so progress is visible.
      if (buildStatus === "queued" || buildStatus === "building") return 10_000;
      if (data?.building_images && data.building_images.length > 0) return 10_000;
      // Otherwise honour the user's chosen dashboard refresh cadence.
      return idleRefetchInterval;
    },
  });

  const hasServerBuilding = (query.data?.building_images ?? []).length > 0;

  const expectedImages = useMemo(
    () => Object.entries(query.data?.expected_image_tags ?? {}),
    [query.data?.expected_image_tags],
  );
  const builtCount = expectedImages.filter(([img, tag]) => {
    const actual = query.data?.actual_tags?.[img] ?? [];
    return actual.includes(tag);
  }).length;
  const totalCount = expectedImages.length;

  const [buildResults, setBuildResults] = useState<BuildResult[]>([]);
  const [buildError, setBuildError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [buildStartTime, setBuildStartTime] = useState<number | null>(null);
  const [singleBuilding, setSingleBuilding] = useState<string | null>(null);

  // Elapsed timer — runs while the task is queued or an ACR run is active.
  useEffect(() => {
    if (buildStatus !== "queued" && buildStatus !== "building") {
      return;
    }
    const start = buildStartTime ?? Date.now();
    if (!buildStartTime) setBuildStartTime(start);
    setElapsed(0);
    const timer = setInterval(
      () => setElapsed(Math.floor((Date.now() - start) / 1000)),
      1000,
    );
    return () => clearInterval(timer);
  }, [buildStatus, buildStartTime]);

  // Auto-dismiss success after 8s
  useEffect(() => {
    if (buildStatus !== "done") return;
    const t = setTimeout(() => setBuildStatus("idle"), 8000);
    return () => clearTimeout(t);
  }, [buildStatus]);

  // If the API accepted the build request but no ACR run ever appears, stop
  // claiming that a build is underway. This is usually a worker/broker problem.
  useEffect(() => {
    if (buildStatus !== "queued" || hasServerBuilding) return;
    if (elapsed < BUILD_QUEUE_TIMEOUT_SECONDS) return;
    setBuildStatus("error");
    setBuildError(
      "Build task was queued, but no ACR run appeared within 5 minutes. Check the worker sidecar and ACR task logs before retrying.",
    );
  }, [buildStatus, elapsed, hasServerBuilding]);

  // Transition queued/building → done when all scheduled builds complete in ACR.
  useEffect(() => {
    if ((buildStatus !== "queued" && buildStatus !== "building") || !buildResults.length)
      return;
    const allScheduled = buildResults.every(
      (r) => r.status === "scheduled" || r.status === "success",
    );
    if (!allScheduled) return;
    const stillBuilding = (query.data?.building_images ?? []).length > 0;
    if (!stillBuilding && query.data) {
      const allBuilt = buildResults.every((r) => {
        const [img, tag] = r.image.split(":");
        return (query.data?.actual_tags?.[img] ?? []).includes(tag);
      });
      if (allBuilt) {
        setBuildStatus("done");
        setElapsed((prev) => prev);
      }
    }
  }, [buildStatus, buildResults, query.data]);

  // Auto-sync: if server shows builds in progress after page refresh, adopt the state.
  useEffect(() => {
    if (hasServerBuilding && (buildStatus === "idle" || buildStatus === "queued")) {
      setBuildStatus("building");
      if (!buildStartTime) setBuildStartTime(Date.now());
    }
    if (!hasServerBuilding && buildStatus === "building" && !singleBuilding) {
      const allBuilt = expectedImages.every(([img, tag]) => {
        return (query.data?.actual_tags?.[img] ?? []).includes(tag as string);
      });
      if (allBuilt) setBuildStatus("done");
    }
  }, [
    hasServerBuilding,
    buildStatus,
    buildStartTime,
    expectedImages,
    singleBuilding,
    query.data,
  ]);

  const handleBuild = async () => {
    setShowConfirm(false);
    setBuildStatus("queued");
    setBuildError(null);
    setBuildResults([]);
    setBuildStartTime(Date.now());
    try {
      const resp = await monitoringApi.buildAcrImages(
        subscriptionId,
        resourceGroup,
        registryName,
      );
      setBuildResults(resp.results);
      const allScheduled = resp.results.every(
        (r) => r.status === "success" || r.status === "scheduled",
      );
      if (allScheduled) {
        setBuildStatus("queued");
      } else {
        setBuildStatus(
          resp.results.every((r) => r.status === "success") ? "done" : "error",
        );
      }
      query.refetch();
    } catch (e) {
      setBuildError(formatApiError(e, "acr"));
      setBuildStatus("error");
    }
  };

  const handleBuildSingle = async (imageName: string) => {
    setSingleBuilding(imageName);
    setBuildStatus("queued");
    setBuildError(null);
    setBuildStartTime(Date.now());
    try {
      const resp = await monitoringApi.buildAcrImages(
        subscriptionId,
        resourceGroup,
        registryName,
        [imageName],
      );
      setBuildResults((prev) => {
        const filtered = prev.filter((r) => !r.image.startsWith(imageName));
        return [...filtered, ...resp.results];
      });
      if (resp.results.some((r) => r.status === "scheduled")) {
        setBuildStatus("queued");
      } else {
        setBuildStatus(
          resp.results.every((r) => r.status === "success") ? "done" : "error",
        );
      }
      query.refetch();
    } catch (e) {
      setBuildError(formatApiError(e, "acr"));
      setBuildStatus("error");
    } finally {
      setSingleBuilding(null);
    }
  };

  return {
    enabled,
    query,
    hasServerBuilding,
    expectedImages,
    builtCount,
    totalCount,
    buildStatus,
    setBuildStatus,
    showConfirm,
    setShowConfirm,
    expandedError,
    setExpandedError,
    buildResults,
    buildError,
    elapsed,
    singleBuilding,
    handleBuild,
    handleBuildSingle,
  } as const;
}

export type AcrBuildsState = ReturnType<typeof useAcrBuilds>;
