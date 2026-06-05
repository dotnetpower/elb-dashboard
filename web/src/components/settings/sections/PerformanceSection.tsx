import { useCallback, useEffect, useState } from "react";

import { formatApiError } from "@/api/client";
import { type AksClusterSummary, monitoringApi } from "@/api/monitoring";
import { settingsApi, type WarmCacheMode } from "@/api/settings";
import type { ResourceConfig } from "@/components/SetupWizard";
import { Badge, Field, Group, Section, StatusLine } from "@/components/settings/primitives";
import { INPUT_STYLE } from "@/components/settings/styles";
import { pickPreferredCluster } from "@/utils/clusterSelection";

/**
 * Performance settings section — per-cluster warm-cache persistence mode.
 *
 * Extracted verbatim from `SettingsPanel.tsx` (issue #24 SRP split). Owns its
 * warm-cache mode catalogue and the sub-wide cluster discovery + read/save
 * flow against `/api/settings/performance`. Backed by `monitoringApi` /
 * `settingsApi`; no panel state.
 */

interface WarmCacheModeMeta {
  id: WarmCacheMode;
  label: string;
  hint: string;
}

const WARM_CACHE_MODE_META: readonly WarmCacheModeMeta[] = [
  {
    id: "ephemeral",
    label: "Ephemeral (default)",
    hint: "Re-download the BLAST database and warm it into RAM on every cluster start. No persistent disk cost; warmup runs in full each time.",
  },
  {
    id: "node_disk",
    label: "Node disk",
    hint: "Persist the staged database on the node's managed OS disk so a stop/start cycle only re-touches RAM instead of re-downloading. Small per-node disk cost.",
  },
  {
    id: "data_disk",
    label: "Dedicated data disk (preview)",
    hint: "Persist the staged database on a dedicated managed data disk (PVC) so it survives node recycling and is decoupled from the OS disk. The cluster is tagged for this mode now; the dedicated-disk warmup path is rolling out and currently falls back to Ephemeral staging.",
  },
];

/**
 * Performance settings — per-cluster warm-cache persistence mode.
 * Writes the `warm_cache_mode` preference via `/api/settings/performance`.
 * The OS/data disk type is fixed at cluster CREATE time, so a change here
 * applies to the NEXT provisioned cluster, not the currently running one.
 * Mirrors AksSection's sub-wide cluster discovery so the dropdown lists
 * clusters outside the dashboard anchor RG.
 */
export function PerformanceSection({ config }: { config: ResourceConfig | null }) {
  const [clusterName, setClusterName] = useState("");
  const [availableClusters, setAvailableClusters] = useState<AksClusterSummary[]>([]);
  const [clustersLoading, setClustersLoading] = useState(false);
  const [clustersLoaded, setClustersLoaded] = useState(false);
  // The mode currently selected in the radio group (editable draft).
  const [selectedMode, setSelectedMode] = useState<WarmCacheMode>("ephemeral");
  // The mode last persisted for this cluster (null = no row, effective default).
  const [savedMode, setSavedMode] = useState<WarmCacheMode | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const selectedClusterRg =
    availableClusters.find((c) => c.name === clusterName)?.resource_group ??
    config?.workloadResourceGroup ??
    "";

  const canRead = Boolean(config?.subscriptionId && selectedClusterRg && clusterName);

  useEffect(() => {
    if (!config?.subscriptionId) return;
    let cancelled = false;
    setClustersLoading(true);
    void (async () => {
      try {
        const response = await monitoringApi.aks(config.subscriptionId);
        if (cancelled) return;
        const clusters = (response.clusters ?? []).filter((c) => c.name);
        setAvailableClusters(clusters);
        setClustersLoaded(true);
        setClusterName((current) => {
          if (current && clusters.some((c) => c.name === current)) return current;
          const preferred = pickPreferredCluster(clusters, {
            resourceGroup: config.workloadResourceGroup,
          });
          return preferred?.name ?? current;
        });
      } catch {
        if (cancelled) return;
        setAvailableClusters([]);
        setClustersLoaded(true);
      } finally {
        if (!cancelled) setClustersLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [config?.subscriptionId, config?.workloadResourceGroup]);

  const refresh = useCallback(async () => {
    if (!config || !canRead) return;
    setError(null);
    setStatus(null);
    setLoading(true);
    try {
      const response = await settingsApi.getPerformance({
        subscription_id: config.subscriptionId,
        resource_group: selectedClusterRg,
        cluster_name: clusterName,
      });
      setSavedMode(response.preference ? response.warm_cache_mode : null);
      setSelectedMode(response.warm_cache_mode);
    } catch (err) {
      setError(formatApiError(err, "aks"));
    } finally {
      setLoading(false);
    }
  }, [canRead, clusterName, config, selectedClusterRg]);

  useEffect(() => {
    if (!canRead) return;
    void refresh();
  }, [canRead, refresh]);

  const save = useCallback(async () => {
    if (!config || !canRead) return;
    setError(null);
    setStatus(null);
    setSaving(true);
    try {
      const response = await settingsApi.putPerformance({
        subscription_id: config.subscriptionId,
        resource_group: selectedClusterRg,
        cluster_name: clusterName,
        warm_cache_mode: selectedMode,
      });
      setSavedMode(response.preference.warm_cache_mode);
      setStatus(`Saved. Applies to the next ${clusterName} provision.`);
    } catch (err) {
      setError(formatApiError(err, "aks"));
    } finally {
      setSaving(false);
    }
  }, [canRead, clusterName, config, selectedClusterRg, selectedMode]);

  const dirty = savedMode !== null ? selectedMode !== savedMode : selectedMode !== "ephemeral";

  return (
    <Section heading="Performance">
      <Group>
        <Field
          label="AKS cluster"
          hint={
            clustersLoading
              ? "Discovering AKS clusters in this subscription..."
              : availableClusters.length > 1
                ? "Pick the cluster whose warm-cache mode you want to configure."
                : clustersLoaded && availableClusters.length === 0
                  ? "No ELB-managed AKS clusters were found. Create one from the Cluster card first."
                  : "The warm-cache mode is stored per cluster."
          }
        >
          {availableClusters.length > 1 ? (
            <select
              value={clusterName}
              onChange={(event) => setClusterName(event.target.value)}
              style={INPUT_STYLE}
            >
              {availableClusters.map((c) => (
                <option key={`${c.resource_group}/${c.name}`} value={c.name}>
                  {c.name} ({c.power_state ?? "?"})
                </option>
              ))}
            </select>
          ) : (
            <input
              value={clusterName}
              onChange={(event) => setClusterName(event.target.value)}
              placeholder={
                clustersLoaded && availableClusters.length === 0
                  ? "No AKS cluster detected"
                  : "aks-..."
              }
              style={INPUT_STYLE}
            />
          )}
        </Field>

        <div role="radiogroup" aria-label="Warm cache mode" style={{ paddingBottom: 6 }}>
          {WARM_CACHE_MODE_META.map((meta) => {
            const checked = selectedMode === meta.id;
            const isSaved = savedMode === meta.id;
            return (
              <label
                key={meta.id}
                style={{
                  display: "flex",
                  gap: 10,
                  alignItems: "flex-start",
                  padding: "12px 0",
                  borderBottom: "1px solid var(--border-weak)",
                  cursor: canRead ? "pointer" : "default",
                  opacity: canRead ? 1 : 0.6,
                }}
              >
                <input
                  type="radio"
                  name="warm-cache-mode"
                  value={meta.id}
                  checked={checked}
                  disabled={!canRead || loading}
                  onChange={() => setSelectedMode(meta.id)}
                  style={{ marginTop: 2 }}
                />
                <div style={{ minWidth: 0 }}>
                  <div
                    style={{
                      fontSize: 13,
                      color: "var(--text-primary)",
                      marginBottom: 2,
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                    }}
                  >
                    {meta.label}
                    {isSaved && <Badge tone="success">Current</Badge>}
                  </div>
                  <div style={{ fontSize: 12, color: "var(--text-faint)", lineHeight: 1.5 }}>
                    {meta.hint}
                  </div>
                </div>
              </label>
            );
          })}
        </div>

        <StatusLine kind="info">
          The disk type is fixed when a cluster is created, so this preference applies to
          the <strong>next</strong> {clusterName ? <code>{clusterName}</code> : "cluster"} provision —
          it does not modify a running cluster.
        </StatusLine>

        <div
          style={{
            display: "flex",
            gap: 8,
            alignItems: "center",
            flexWrap: "wrap",
            padding: "14px 0",
          }}
        >
          <button
            className="glass-button glass-button--primary"
            onClick={save}
            disabled={!canRead || saving || loading || !dirty}
            style={{ fontSize: 12 }}
          >
            {saving ? "Saving..." : "Save preference"}
          </button>
          <button
            className="glass-button"
            onClick={refresh}
            disabled={!canRead || loading}
            style={{ fontSize: 12 }}
          >
            {loading ? "Loading..." : "Refresh"}
          </button>
          {savedMode === null && canRead && !loading && (
            <span style={{ fontSize: 12, color: "var(--text-faint)" }}>
              No preference set — defaults to Ephemeral.
            </span>
          )}
        </div>
        {status && <StatusLine kind="success">{status}</StatusLine>}
        {error && <StatusLine kind="error">{error}</StatusLine>}
      </Group>
    </Section>
  );
}
