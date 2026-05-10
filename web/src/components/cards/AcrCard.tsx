import { useState, useEffect, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Loader2, Hammer, CheckCircle2, AlertTriangle } from "lucide-react";

import { monitoringApi } from "@/api/endpoints";
import { MonitorCard } from "@/components/MonitorCard";
import { useRefreshCountdown } from "@/hooks/useRefreshCountdown";

// Short display names for long image paths
const SHORT_NAMES: Record<string, string> = {
  "ncbi/elb": "elb (BLAST worker)",
  "ncbi/elasticblast-job-submit": "job-submit",
  "ncbi/elasticblast-query-split": "query-split",
  "elb-openapi": "openapi",
};

interface Props {
  subscriptionId: string;
  resourceGroup: string;
  registryName: string;
}

export function AcrCard({ subscriptionId, resourceGroup, registryName }: Props) {
  const enabled = Boolean(subscriptionId && resourceGroup && registryName);
  const [buildStatus, setBuildStatus] = useState<"idle" | "building" | "done" | "error">("idle");
  const [showConfirm, setShowConfirm] = useState(false);
  const [expandedError, setExpandedError] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ["acr", subscriptionId, resourceGroup, registryName],
    queryFn: () => monitoringApi.acr(subscriptionId, resourceGroup, registryName),
    enabled,
    refetchInterval: (q) => {
      const data = q.state.data;
      if (buildStatus === "building") return 10_000;
      if (data?.building_images && data.building_images.length > 0) return 10_000;
      return 60_000;
    },
  });

  const hasServerBuilding = (query.data?.building_images ?? []).length > 0;
  const currentInterval = useMemo(() => {
    if (buildStatus === "building") return 10_000;
    if (hasServerBuilding) return 10_000;
    return 60_000;
  }, [buildStatus, hasServerBuilding]);
  const refreshCountdown = useRefreshCountdown(query.dataUpdatedAt, currentInterval);
  const expectedImages = Object.entries(query.data?.expected_image_tags ?? {});
  const builtCount = expectedImages.filter(([img, tag]) => {
    const actual = query.data?.actual_tags?.[img] ?? [];
    return actual.includes(tag);
  }).length;
  const totalCount = expectedImages.length;

  const [buildResults, setBuildResults] = useState<{ image: string; status: string; error?: string; run_id?: string }[]>([]);
  const [buildError, setBuildError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);

  // #18: Auto-dismiss success after 8s
  useEffect(() => {
    if (buildStatus !== "done") return;
    const t = setTimeout(() => setBuildStatus("idle"), 8000);
    return () => clearTimeout(t);
  }, [buildStatus]);

  const handleBuild = async () => {
    setShowConfirm(false);
    setBuildStatus("building");
    setBuildError(null);
    setBuildResults([]);
    const start = Date.now();
    const timer = setInterval(() => setElapsed(Math.floor((Date.now() - start) / 1000)), 1000);
    try {
      const resp = await monitoringApi.buildAcrImages(subscriptionId, resourceGroup, registryName);
      setBuildResults(resp.results);
      setBuildStatus(resp.results.every((r) => r.status === "success") ? "done" : "error");
      query.refetch();
    } catch (e) {
      setBuildError((e as Error).message);
      setBuildStatus("error");
    } finally {
      clearInterval(timer);
    }
  };

  const formatTime = (s: number) => `${Math.floor(s / 60)}m ${s % 60}s`;

  const status = !enabled
    ? "idle"
    : query.isLoading
      ? "loading"
      : query.isError
        ? "error"
        : "ready";

  return (
    <MonitorCard
      title="ACR"
      subtitle={enabled ? `${registryName} · ${resourceGroup}` : "Configure ACR name"}
      status={buildStatus === "building" || hasServerBuilding ? "loading" : status}
      lastRefreshed={query.dataUpdatedAt ? new Date(query.dataUpdatedAt) : null}
      refreshCountdown={refreshCountdown}
      refreshInterval={currentInterval}
      onRefresh={() => query.refetch()}
      accentColor="acr"
      collapsible
      rightSlot={
        enabled && (
          <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
            {/* #8: Confirm dialog before build */}
            {buildStatus !== "building" && !hasServerBuilding && (
              <button className="glass-button glass-button--primary" onClick={() => setShowConfirm(true)} style={{ fontSize: 10 }}>
                <Hammer size={11} strokeWidth={1.5} /> Build
              </button>
            )}
          </div>
        )
      }
    >
      {!enabled && <div className="muted">Set Subscription ID, ACR RG, and ACR Name above.</div>}
      {query.isError && <div className="muted">Failed: {(query.error as Error).message}</div>}

      {query.data && (
        <>
          {/* #11: Grid layout for registry metadata */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(100px, 1fr))", gap: "var(--space-2)", fontSize: 12, marginBottom: "var(--space-3)" }}>
            <div>
              <div className="muted" style={{ fontSize: 10, textTransform: "uppercase" }}>Login Server</div>
              <div style={{ fontSize: 11, wordBreak: "break-all" }}>{query.data.login_server}</div>
            </div>
            <div>
              <div className="muted" style={{ fontSize: 10, textTransform: "uppercase" }}>SKU</div>
              <div>{query.data.sku ?? "?"}</div>
            </div>
            <div>
              <div className="muted" style={{ fontSize: 10, textTransform: "uppercase" }}>Images</div>
              {/* #15: Progress indicator */}
              <div style={{ color: builtCount === totalCount ? "var(--success)" : "var(--text-primary)", fontWeight: 600 }}>
                {builtCount}/{totalCount} built
              </div>
            </div>
          </div>

          {/* #15: Progress bar */}
          {totalCount > 0 && (
            <div style={{ height: 3, background: "var(--border-weak)", borderRadius: 2, marginBottom: "var(--space-3)", overflow: "hidden" }}>
              <div style={{ height: "100%", width: `${(builtCount / totalCount) * 100}%`, background: builtCount === totalCount ? "var(--success)" : "var(--accent)", borderRadius: 2, transition: "width 0.3s ease" }} />
            </div>
          )}

          {/* Image table */}
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border-weak)" }}>
                {/* #14: Shortened "Image" header */}
                <th style={{ textAlign: "left", padding: "4px 0", color: "var(--text-faint)", fontSize: 10, textTransform: "uppercase", fontWeight: 500 }}>Image</th>
                {/* #13: "Version" instead of "Expected tag" */}
                <th style={{ padding: "4px 0", color: "var(--text-faint)", fontSize: 10, textTransform: "uppercase", fontWeight: 500 }}>Version</th>
                <th style={{ textAlign: "right", padding: "4px 0", color: "var(--text-faint)", fontSize: 10, textTransform: "uppercase", fontWeight: 500 }}>Status</th>
              </tr>
            </thead>
            <tbody>
              {expectedImages.map(([img, tag]) => {
                const result = buildResults.find((r) => r.image === `${img}:${tag}`);
                const actualTags = query.data?.actual_tags?.[img] ?? [];
                const isBuilt = actualTags.includes(tag);
                const isBuilding = buildStatus === "building" || (query.data?.building_images ?? []).includes(`${img}:${tag}`);
                const shortName = SHORT_NAMES[img] || img.split("/").pop() || img;
                const isFailed = result?.status === "failed";

                return (
                  <tr key={img} style={{ borderBottom: "1px solid var(--border-weak)" }}>
                    {/* #14: Short name with full path as title */}
                    <td style={{ padding: "6px 0" }} title={img}>
                      <strong style={{ fontSize: 12 }}>{shortName}</strong>
                    </td>
                    <td style={{ padding: "6px 0", textAlign: "center" }}>
                      <code style={{ fontSize: 11 }}>{tag}</code>
                    </td>
                    <td style={{ padding: "6px 0", textAlign: "right" }}>
                      {/* #1: Multi-state badges */}
                      {isBuilding && !result ? (
                        <span style={{ color: "var(--accent)", fontSize: 10, display: "inline-flex", alignItems: "center", gap: 3 }}>
                          <Loader2 size={10} className="spin" /> Building
                        </span>
                      ) : result?.status === "success" || isBuilt ? (
                        <span className="gt gt-g" style={{ fontSize: 9 }}>Built</span>
                      ) : isFailed ? (
                        <button
                          onClick={() => setExpandedError(expandedError === img ? null : img)}
                          style={{ background: "none", border: "none", cursor: "pointer", padding: 0 }}
                        >
                          <span className="gt gt-r" style={{ fontSize: 9 }}>Failed ▾</span>
                        </button>
                      ) : (
                        <span className="gt gt-m" style={{ fontSize: 9 }}>Not Built</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          {/* #17: Expandable error details */}
          {expandedError && (() => {
            const r = buildResults.find((r) => r.image.startsWith(expandedError));
            return r?.error ? (
              <div style={{ marginTop: "var(--space-2)", padding: "6px 10px", background: "rgba(224,123,138,0.06)", border: "1px solid rgba(224,123,138,0.15)", borderRadius: 6, fontSize: 10, color: "var(--danger)", fontFamily: "var(--font-mono)", whiteSpace: "pre-wrap", maxHeight: 120, overflow: "auto" }}>
                {r.error}
              </div>
            ) : null;
          })()}
        </>
      )}

      {/* #8: Build confirmation */}
      {showConfirm && (
        <div style={{ marginTop: "var(--space-3)", padding: "10px 14px", background: "rgba(110,159,255,0.06)", border: "1px solid rgba(110,159,255,0.2)", borderRadius: 8, fontSize: 12 }}>
          <div style={{ fontWeight: 600, marginBottom: 6, color: "var(--accent)" }}>
            <Hammer size={14} style={{ verticalAlign: "middle", marginRight: 4 }} />
            Build {totalCount} images?
          </div>
          <div className="muted" style={{ fontSize: 11, marginBottom: 8 }}>
            Images will be built from GitHub via ACR Build Tasks. Estimated time: ~5-15 min per image ({totalCount * 10}+ min total).
            {builtCount > 0 && ` ${builtCount} already built will be rebuilt.`}
          </div>
          <div style={{ display: "flex", gap: "var(--space-2)" }}>
            <button className="glass-button glass-button--primary" onClick={handleBuild} style={{ fontSize: 11 }}>
              <Hammer size={11} /> Start Build
            </button>
            <button className="glass-button" onClick={() => setShowConfirm(false)} style={{ fontSize: 11 }}>Cancel</button>
          </div>
        </div>
      )}

      {/* Build progress */}
      {buildStatus === "building" && (
        <div style={{ marginTop: "var(--space-3)", padding: "6px 10px", background: "rgba(110,159,255,0.06)", border: "1px solid rgba(110,159,255,0.15)", borderRadius: 6, fontSize: 11, color: "var(--accent)" }}>
          <Loader2 size={11} className="spin" style={{ display: "inline", verticalAlign: "middle", marginRight: 4 }} />
          Building via ACR... {formatTime(elapsed)}
        </div>
      )}

      {/* Build success */}
      {buildStatus === "done" && (
        <div style={{ marginTop: "var(--space-3)", fontSize: 11, color: "var(--success)" }}>
          <CheckCircle2 size={11} style={{ verticalAlign: "middle" }} /> All images built in {formatTime(elapsed)}
        </div>
      )}

      {/* Build error (global) */}
      {buildError && (
        <div style={{ marginTop: "var(--space-3)", fontSize: 11, color: "var(--danger)" }}>
          <AlertTriangle size={11} style={{ verticalAlign: "middle" }} /> {buildError}
        </div>
      )}

      {/* #10: Server-side build in progress message */}
      {hasServerBuilding && buildStatus === "idle" && (
        <div style={{ marginTop: "var(--space-3)", padding: "6px 10px", background: "rgba(110,159,255,0.06)", border: "1px solid rgba(110,159,255,0.15)", borderRadius: 6, fontSize: 11, color: "var(--accent)" }}>
          <Loader2 size={11} className="spin" style={{ display: "inline", verticalAlign: "middle", marginRight: 4 }} />
          Build in progress (started externally). Auto-refreshing...
        </div>
      )}
    </MonitorCard>
  );
}
