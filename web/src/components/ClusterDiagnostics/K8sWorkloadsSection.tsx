import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, Loader2 } from "lucide-react";

import { monitoringApi } from "@/api/endpoints";

import { K8sDeploymentsPanel } from "./K8sDeploymentsPanel";
import { K8sJobsPanel } from "./K8sJobsPanel";
import { K8sPodsPanel } from "./K8sPodsPanel";
import { SectionShimmerBar } from "./SectionShimmerBar";

/**
 * Collapsible "Workloads" card that groups the cluster's Pods, Deployments
 * and Jobs into a tabbed view — mirroring the Azure portal Workloads pane.
 * Owns the collapse + active-tab state and the per-tab data queries; each
 * tab body is rendered by its own panel component. Node-level diagnostics
 * (Node Resources / Nodes) stay as sibling stacked sections in the parent,
 * matching the portal's split between infrastructure and workloads.
 *
 * Queries are lazy: a tab only fetches once the card is expanded and that
 * tab is active, so opening the modal does not fan out three Kubernetes API
 * calls. The shared `["aks-workload", …]` query-key prefix lets the parent's
 * "Refresh All" button invalidate whichever tab is currently live.
 */
type WorkloadTab = "pods" | "deployments" | "jobs";

export function K8sWorkloadsSection({
  subscriptionId,
  resourceGroup,
  clusterName,
}: {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
}) {
  const [collapsed, setCollapsed] = useState(true);
  const [tab, setTab] = useState<WorkloadTab>("pods");

  const podsQuery = useQuery({
    queryKey: ["aks-workload", "pods", subscriptionId, resourceGroup, clusterName],
    queryFn: () => monitoringApi.k8sPods(subscriptionId, resourceGroup, clusterName),
    enabled: !collapsed && tab === "pods",
    staleTime: 60_000,
    retry: 1,
  });
  const deploymentsQuery = useQuery({
    queryKey: ["aks-workload", "deployments", subscriptionId, resourceGroup, clusterName],
    queryFn: () => monitoringApi.k8sDeployments(subscriptionId, resourceGroup, clusterName),
    enabled: !collapsed && tab === "deployments",
    staleTime: 60_000,
    retry: 1,
  });
  const jobsQuery = useQuery({
    queryKey: ["aks-workload", "jobs", subscriptionId, resourceGroup, clusterName],
    queryFn: () => monitoringApi.k8sJobs(subscriptionId, resourceGroup, clusterName),
    enabled: !collapsed && tab === "jobs",
    staleTime: 60_000,
    retry: 1,
  });

  const activeFetching =
    (tab === "pods" && podsQuery.isFetching) ||
    (tab === "deployments" && deploymentsQuery.isFetching) ||
    (tab === "jobs" && jobsQuery.isFetching);

  const counts: Record<WorkloadTab, number | undefined> = {
    pods: podsQuery.data?.pods.length,
    deployments: deploymentsQuery.data?.deployments.length,
    jobs: jobsQuery.data?.jobs.length,
  };

  const tabs: { id: WorkloadTab; label: string }[] = [
    { id: "pods", label: "Pods" },
    { id: "deployments", label: "Deployments" },
    { id: "jobs", label: "Jobs" },
  ];

  return (
    <div
      style={{
        position: "relative",
        borderRadius: 8,
        border: "1px solid var(--border-weak)",
        overflow: "hidden",
      }}
    >
      <SectionShimmerBar active={Boolean(activeFetching)} />
      <button
        onClick={() => setCollapsed(!collapsed)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          width: "100%",
          background: collapsed ? "transparent" : "var(--bg-tertiary)",
          border: "none",
          color: "var(--text-primary)",
          cursor: "pointer",
          padding: "8px 12px",
          fontSize: 11,
          textAlign: "left",
          fontWeight: 500,
        }}
      >
        <ChevronDown
          size={12}
          style={{
            transform: collapsed ? "rotate(-90deg)" : "rotate(0deg)",
            color: "var(--text-faint)",
            transition: "transform 0.15s",
          }}
        />
        Workloads
        {activeFetching && (
          <Loader2
            size={10}
            className="spin"
            style={{ marginLeft: "auto", color: "var(--accent)" }}
          />
        )}
      </button>
      {!collapsed && (
        <div style={{ borderTop: "1px solid var(--border-weak)" }}>
          <div
            role="tablist"
            aria-label="Cluster workloads"
            style={{
              display: "flex",
              gap: 4,
              padding: "6px 8px",
              background: "var(--bg-tertiary)",
              borderBottom: "1px solid var(--border-weak)",
            }}
          >
            {tabs.map((t) => {
              const active = tab === t.id;
              const count = counts[t.id];
              return (
                <button
                  key={t.id}
                  role="tab"
                  aria-selected={active}
                  onClick={() => setTab(t.id)}
                  className="glass-button"
                  style={{
                    padding: "4px 10px",
                    fontSize: 10,
                    fontWeight: active ? 600 : 500,
                    color: active ? "var(--text-primary)" : "var(--text-muted)",
                    background: active ? "var(--bg-secondary)" : "transparent",
                    borderColor: active ? "var(--teal)" : "var(--border-weak)",
                  }}
                >
                  {t.label}
                  {count !== undefined && (
                    <span className="muted" style={{ marginLeft: 5, fontSize: 9 }}>
                      {count}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
          {tab === "pods" && (
            <K8sPodsPanel
              query={podsQuery}
              subscriptionId={subscriptionId}
              resourceGroup={resourceGroup}
              clusterName={clusterName}
            />
          )}
          {tab === "deployments" && (
            <K8sDeploymentsPanel
              query={deploymentsQuery}
              subscriptionId={subscriptionId}
              resourceGroup={resourceGroup}
              clusterName={clusterName}
            />
          )}
          {tab === "jobs" && (
            <K8sJobsPanel
              query={jobsQuery}
              subscriptionId={subscriptionId}
              resourceGroup={resourceGroup}
              clusterName={clusterName}
            />
          )}
        </div>
      )}
    </div>
  );
}
