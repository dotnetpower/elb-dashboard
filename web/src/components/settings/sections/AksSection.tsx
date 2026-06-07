import { useCallback, useEffect, useState } from "react";

import { formatApiError } from "@/api/client";
import { type AksClusterSummary, monitoringApi } from "@/api/monitoring";
import { settingsApi } from "@/api/settings";
import type { ResourceConfig } from "@/components/SetupWizard";
import { Badge, Field, Group, Row, Section, StatusLine } from "@/components/settings/primitives";
import { INPUT_STYLE, SELECT_STYLE } from "@/components/settings/styles";
import { isRunningTask, usePollTask, TaskStatusLine, type TaskState } from "@/components/settings/taskState";
import { usePreferences } from "@/hooks/usePreferences";
import { pickPreferredCluster } from "@/utils/clusterSelection";

/**
 * AKS Observability settings section — Container Insights (omsagent) enable /
 * disable for a discovered cluster.
 *
 * Extracted verbatim from `SettingsPanel.tsx` (issue #24 SRP split). Owns the
 * sub-wide cluster discovery, the App-Insights-name → Log-Analytics-workspace
 * resolution, and the enable/disable background tasks. Backed by
 * `monitoringApi` / `settingsApi` / `usePreferences`.
 */
export function AksSection({ config }: { config: ResourceConfig | null }) {
  const { prefs, setPref } = usePreferences();
  const [clusterName, setClusterName] = useState("");
  const [appInsightsName, setAppInsightsName] = useState("appi-elb-dashboard");
  const [status, setStatus] = useState<string | null>(null);
  const [containerInsightsEnabled, setContainerInsightsEnabled] = useState<boolean | null>(null);
  const [task, setTask] = useState<TaskState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resolvingWorkspace, setResolvingWorkspace] = useState(false);
  // Track the full cluster objects (not just names) so enable/disable/status
  // requests can forward each cluster's actual `resource_group`. A multi-tier
  // fleet routinely lives across several RGs (e.g. workload RG vs the
  // elastic-blast default RG) and the backend `list_aks_clusters` lookup is
  // RG-scoped, so sending the workspace anchor RG for a cluster that lives
  // elsewhere produces a "cluster not found" failure.
  const [availableClusters, setAvailableClusters] = useState<AksClusterSummary[]>([]);
  const [clustersLoading, setClustersLoading] = useState(false);
  const [clustersLoaded, setClustersLoaded] = useState(false);

  usePollTask(task, setTask, (taskStatus) => {
    if (taskStatus.status !== "SUCCESS") return;
    const result = taskStatus.result as { enabled?: boolean; workspace_resource_id?: string | null } | null;
    if (typeof result?.enabled === "boolean") {
      setContainerInsightsEnabled(result.enabled);
      setStatus(
        result.enabled
          ? `Enabled (${result.workspace_resource_id ?? "workspace unknown"})`
          : "Disabled",
      );
    }
  });

  // Resolve the selected cluster's *actual* RG. The dropdown stores the
  // cluster name but the backend Observability endpoints need the RG that
  // physically holds the AKS resource (api/services/aks_observability.py
  // calls `client.managed_clusters.get(rg, name)` directly).
  const selectedClusterRg =
    availableClusters.find((c) => c.name === clusterName)?.resource_group ??
    config?.workloadResourceGroup ??
    "";

  const canRead = Boolean(
    config?.subscriptionId && selectedClusterRg && clusterName,
  );

  const refresh = useCallback(async () => {
    if (!config || !canRead) return;
    setError(null);
    try {
      const response = await settingsApi.getAksObservabilityStatus({
        subscription_id: config.subscriptionId,
        resource_group: selectedClusterRg,
        cluster_name: clusterName,
      });
      setContainerInsightsEnabled(response.enabled);
      setStatus(response.enabled ? `Enabled (${response.workspace_resource_id ?? "workspace unknown"})` : "Disabled");
      if (response.workspace_resource_id) {
        setPref("appInsightsWorkspaceResourceId", response.workspace_resource_id);
      }
    } catch (err) {
      setError(formatApiError(err, "aks"));
    }
  }, [canRead, clusterName, config, selectedClusterRg, setPref]);

  useEffect(() => {
    if (!canRead) return;
    void refresh();
  }, [canRead, refresh]);

  useEffect(() => {
    // Sub-wide cluster discovery — matches ClusterCard / StorageCard /
    // BlastSubmit so an ElasticBLAST workload cluster living outside the
    // dashboard anchor RG is still listed in the Observability picker.
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

  const workspaceId = prefs.appInsightsWorkspaceResourceId.trim();

  const resolveWorkspace = useCallback(async (): Promise<string> => {
    if (!config) return "";
    setError(null);
    setResolvingWorkspace(true);
    try {
      const { component } = await settingsApi.lookupAppInsights({
        subscription_id: config.subscriptionId,
        component_name: appInsightsName,
      });
      if (component.connection_string) {
        setPref("appInsightsConnectionString", component.connection_string);
        setPref("telemetryEnabled", true);
      }
      if (!component.workspace_resource_id) {
        setError("This App Insights resource did not return a Log Analytics workspace id.");
        return "";
      }
      setPref("appInsightsWorkspaceResourceId", component.workspace_resource_id);
      setStatus(`Workspace resolved (${component.workspace_resource_id.split("/").slice(-1)[0]})`);
      return component.workspace_resource_id;
    } catch (err) {
      setError(formatApiError(err, "arm"));
      return "";
    } finally {
      setResolvingWorkspace(false);
    }
  }, [appInsightsName, config, setPref]);

  const enable = useCallback(async () => {
    if (!config) return;
    setError(null);
    setTask(null);
    // Always re-resolve the workspace from the named App Insights component
    // instead of trusting the stored pref. The workspace is derived solely
    // from the component (there is no manual workspace-id field), so a stale
    // cached id — e.g. a default workspace captured before the component was
    // re-pointed — must never be sent to the omsagent patch. resolveWorkspace
    // refreshes the pref so the display and the request stay consistent.
    const workspaceId = await resolveWorkspace();
    if (!workspaceId) return;
    try {
      const response = await settingsApi.enableAksObservability({
        subscription_id: config.subscriptionId,
        resource_group: selectedClusterRg,
        cluster_name: clusterName,
        workspace_resource_id: workspaceId,
      });
      setTask({ taskId: response.task_id, status: "PENDING" });
    } catch (err) {
      setError(formatApiError(err, "aks"));
    }
  }, [clusterName, config, resolveWorkspace, selectedClusterRg]);

  const disable = useCallback(async () => {
    if (!config) return;
    setError(null);
    setTask(null);
    try {
      const response = await settingsApi.disableAksObservability({
        subscription_id: config.subscriptionId,
        resource_group: selectedClusterRg,
        cluster_name: clusterName,
      });
      setTask({ taskId: response.task_id, status: "PENDING", message: "Disabling Container Insights" });
    } catch (err) {
      setError(formatApiError(err, "aks"));
    }
  }, [clusterName, config, selectedClusterRg]);

  return (
    <Section heading="AKS Observability">
      <Group>
        <Field
          label="AKS cluster name"
          hint={
            clustersLoading
              ? "Discovering AKS clusters in this subscription..."
              : availableClusters.length > 1
                ? "Pick the cluster whose omsagent addon should be patched."
                : availableClusters.length === 1
                  ? "Container Insights is enabled by patching the omsagent addon on this cluster."
                  : clustersLoaded
                    ? "No ELB-managed AKS clusters were found in this subscription. Create one from the Cluster card first."
                    : "Container Insights is enabled by patching the omsagent addon on this cluster."
          }
        >
          {availableClusters.length > 1 ? (
            <select
              value={clusterName}
              onChange={(event) => setClusterName(event.target.value)}
              style={SELECT_STYLE}
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
              placeholder={clustersLoaded && availableClusters.length === 0 ? "No AKS cluster detected" : "aks-..."}
              style={INPUT_STYLE}
            />
          )}
        </Field>
        <Field label="Application Insights resource name" hint="Used to resolve the backing Log Analytics workspace automatically.">
          <input value={appInsightsName} onChange={(event) => setAppInsightsName(event.target.value)} style={INPUT_STYLE} placeholder="appi-elb-dashboard" />
        </Field>
        <Row
          label="Log Analytics workspace"
          hint={workspaceId ? "Automatically captured from the App Insights resource." : "Use Telemetry > Provision a resource first. Existing App Insights resources are reused by name."}
          control={<Badge tone={workspaceId ? "success" : "muted"}>{workspaceId ? "Ready" : "Missing"}</Badge>}
        />
        {workspaceId && (
          <StatusLine kind="info">
            Workspace <code>{workspaceId.split("/").slice(-1)[0]}</code> will be used.
          </StatusLine>
        )}
        {!workspaceId && prefs.appInsightsConnectionString && (
          <StatusLine kind="info">
            The connection string is configured, but AKS needs the backing Log Analytics workspace. Open Telemetry and provision/reuse the App Insights resource by name to fill it automatically.
          </StatusLine>
        )}
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", paddingBottom: 14 }}>
          <button className="glass-button" onClick={resolveWorkspace} disabled={!canRead || !appInsightsName || resolvingWorkspace} style={{ fontSize: 12 }}>
            {resolvingWorkspace ? "Resolving..." : "Resolve workspace"}
          </button>
          <button className="glass-button" onClick={refresh} disabled={!canRead} style={{ fontSize: 12 }}>Refresh status</button>
          {containerInsightsEnabled ? (
            <button className="glass-button" onClick={disable} disabled={!canRead || isRunningTask(task)} style={{ fontSize: 12 }}>Disable Container Insights</button>
          ) : (
            <button className="glass-button glass-button--primary" onClick={enable} disabled={!canRead || !appInsightsName || isRunningTask(task) || resolvingWorkspace} style={{ fontSize: 12 }}>Enable Container Insights</button>
          )}
        </div>
        {status && <StatusLine kind={status.startsWith("Enabled") ? "success" : "info"}>{status}</StatusLine>}
        {error && <StatusLine kind="error">{error}</StatusLine>}
        {task && <TaskStatusLine task={task} />}
      </Group>
    </Section>
  );
}
