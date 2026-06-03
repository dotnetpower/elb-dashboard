import { Loader2 } from "lucide-react";

import { monitoringApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import type { K8sPod } from "@/api/endpoints";

import { formatAge } from "./k8sFormat";
import { NamespaceFilter } from "./NamespaceFilter";
import { useNamespaceFilter } from "./useNamespaceFilter";
import { useWorkloadActions } from "./useWorkloadActions";

/**
 * Pods tab of the cluster Workloads card. Lists pods of every phase
 * (Pending, Running, Succeeded/Completed, Failed) across all namespaces —
 * mirroring the Azure portal Pods view — with a namespace filter and
 * Node / Pod IP columns. The per-row action lifecycle (Logs / Describe /
 * Delete) is shared with the Deployments / Jobs tabs via
 * `useWorkloadActions`; the Delete action gates on system-managed namespaces
 * both there (button hidden) and on the backend route
 * (`/api/monitor/aks/pod` returns 403), the SPA-side gate being convenience.
 * The collapse chrome lives in the parent `K8sWorkloadsSection`.
 */
export interface K8sPodsQuery {
  isLoading: boolean;
  isFetching?: boolean;
  isError: boolean;
  data?: { pods: K8sPod[] } | null;
  error?: unknown;
  refetch?: () => void;
}

export function K8sPodsPanel({
  query,
  subscriptionId,
  resourceGroup,
  clusterName,
}: {
  query: K8sPodsQuery;
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
}) {
  const allPods = query.data?.pods ?? [];
  const { effectiveNs, setNsFilter, namespaces, filtered: pods } =
    useNamespaceFilter(allPods);

  const actions = useWorkloadActions(
    "Pod",
    {
      logs: (ns, name) =>
        monitoringApi.k8sPodLogs(subscriptionId, resourceGroup, clusterName, ns, name, 200),
      describe: (ns, name) =>
        monitoringApi.k8sPodDescribe(subscriptionId, resourceGroup, clusterName, ns, name),
      del: (ns, name) =>
        monitoringApi.k8sPodDelete(subscriptionId, resourceGroup, clusterName, ns, name),
    },
    // The backend already invalidated the pods snapshot cache so the next
    // refetch returns fresh state.
    () => query.refetch?.(),
    {
      details: [
        "A controller (Deployment / ReplicaSet / StatefulSet) may recreate it on its own — this is normal Kubernetes behaviour.",
        "Pods backed by a Job will not restart; the Job will be marked failed if no other pods complete the work.",
      ],
      footnote:
        "Logs from this pod will be lost unless they have already been shipped off-cluster.",
    },
  );

  const sc = (s: string) => {
    const v = s.toLowerCase();
    return v === "running"
      ? "var(--success)"
      : v.includes("error") || v.includes("crash")
        ? "var(--danger)"
        : "var(--warning)";
  };

  return (
    <div className="k8s-pods-table-wrap" style={{ overflowX: "auto" }}>
      {query.isLoading && (
        <div style={{ padding: 16, textAlign: "center" }} className="muted">
          <Loader2 size={14} className="spin" /> Loading...
        </div>
      )}
      {query.isError && (
        <div style={{ padding: 12, fontSize: 11, color: "var(--danger)" }}>
          {formatApiError(query.error, "aks")}
        </div>
      )}
      <NamespaceFilter
        idSuffix="pods"
        items={allPods}
        namespaces={namespaces}
        value={effectiveNs}
        onChange={setNsFilter}
        shown={pods.length}
      />
      {!query.isLoading && !query.isError && allPods.length === 0 && (
        <div style={{ padding: 12, fontSize: 11 }} className="muted">
          No pods found.
        </div>
      )}
      {pods.length > 0 && (
        <table
          style={{
            width: "100%",
            fontSize: 10,
            borderCollapse: "collapse",
            fontFamily: "var(--font-mono)",
          }}
        >
          <thead>
            <tr style={{ background: "var(--bg-tertiary)" }}>
              {["NS", "NAME", "READY", "STATUS", "RESTARTS", "AGE", "NODE", "POD IP", ""].map(
                (h) => (
                  <th
                    key={h}
                    style={{
                      textAlign: "left",
                      padding: "6px 8px",
                      color: "var(--text-faint)",
                      fontSize: 9,
                      textTransform: "uppercase",
                      fontWeight: 500,
                    }}
                  >
                    {h}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {pods.map((p, i) => (
              <tr
                key={`${p.namespace}/${p.name}`}
                style={{
                  background: i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.012)",
                  borderTop: "1px solid var(--border-weak)",
                }}
              >
                <td
                  style={{
                    padding: "5px 8px",
                    color: "var(--text-muted)",
                    fontSize: 9,
                  }}
                >
                  {p.namespace}
                </td>
                <td style={{ padding: "5px 8px", fontWeight: 500 }}>{p.name}</td>
                <td style={{ padding: "5px 8px" }}>{p.ready}</td>
                <td style={{ padding: "5px 8px", color: sc(p.status) }}>
                  <span
                    style={{
                      display: "inline-block",
                      width: 5,
                      height: 5,
                      borderRadius: "50%",
                      background: sc(p.status),
                      marginRight: 4,
                      verticalAlign: "middle",
                    }}
                  />
                  {p.status}
                </td>
                <td style={{ padding: "5px 8px" }}>{p.restarts}</td>
                <td
                  style={{
                    padding: "5px 8px",
                    color: "var(--text-muted)",
                    fontSize: 9,
                    whiteSpace: "nowrap",
                  }}
                  title={p.age || undefined}
                >
                  {formatAge(p.age)}
                </td>
                <td
                  style={{
                    padding: "5px 8px",
                    color: "var(--text-muted)",
                    fontSize: 9,
                  }}
                  title={p.node_ip ? `${p.node} · ${p.node_ip}` : p.node || undefined}
                >
                  {p.node ? p.node.split("-vmss")[0] : "—"}
                </td>
                <td
                  style={{
                    padding: "5px 8px",
                    color: "var(--text-muted)",
                    fontSize: 9,
                    whiteSpace: "nowrap",
                  }}
                  title={p.node_ip ? `Node IP: ${p.node_ip}` : undefined}
                >
                  {p.pod_ip || "—"}
                </td>
                <td style={{ padding: "4px 8px" }}>
                  {actions.renderActions(p.namespace, p.name)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {actions.dialogs}
    </div>
  );
}
