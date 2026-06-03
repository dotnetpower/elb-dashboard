import { Loader2 } from "lucide-react";

import { monitoringApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import type { K8sDeployment } from "@/api/endpoints";

import { formatAge } from "./k8sFormat";
import { NamespaceFilter } from "./NamespaceFilter";
import { useNamespaceFilter } from "./useNamespaceFilter";
import { useWorkloadActions } from "./useWorkloadActions";

/**
 * Deployments tab of the cluster Workloads card. Table mirroring the Azure
 * portal Deployments view (Ready / Up-to-date / Available / Age) with the
 * same namespace filter as the Pods tab, plus the shared per-row Logs /
 * Describe / Delete actions (`useWorkloadActions`). Delete deletes the
 * Deployment with Foreground propagation; the backend route gates system
 * namespaces. The collapse chrome lives in the parent `K8sWorkloadsSection`.
 */
export interface K8sDeploymentsQuery {
  isLoading: boolean;
  isFetching?: boolean;
  isError: boolean;
  data?: { deployments: K8sDeployment[] } | null;
  error?: unknown;
  refetch?: () => void;
}

const HEADERS = ["NS", "NAME", "READY", "UP-TO-DATE", "AVAILABLE", "AGE", ""];

export function K8sDeploymentsPanel({
  query,
  subscriptionId,
  resourceGroup,
  clusterName,
}: {
  query: K8sDeploymentsQuery;
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
}) {
  const all = query.data?.deployments ?? [];
  const { effectiveNs, setNsFilter, namespaces, filtered } = useNamespaceFilter(all);

  const actions = useWorkloadActions(
    "Deployment",
    {
      logs: (ns, name) =>
        monitoringApi.k8sDeploymentLogs(
          subscriptionId,
          resourceGroup,
          clusterName,
          ns,
          name,
          200,
        ),
      describe: (ns, name) =>
        monitoringApi.k8sDeploymentDescribe(
          subscriptionId,
          resourceGroup,
          clusterName,
          ns,
          name,
        ),
      del: (ns, name) =>
        monitoringApi.k8sDeploymentDelete(
          subscriptionId,
          resourceGroup,
          clusterName,
          ns,
          name,
        ),
    },
    () => query.refetch?.(),
    {
      details: [
        "All pods managed by this Deployment will be terminated.",
        "If a higher-level controller (e.g. a Helm release or operator) owns it, the Deployment may be recreated.",
      ],
      footnote: "This stops the workload until it is redeployed.",
    },
  );

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
        idSuffix="deployments"
        items={all}
        namespaces={namespaces}
        value={effectiveNs}
        onChange={setNsFilter}
        shown={filtered.length}
      />
      {!query.isLoading && !query.isError && all.length === 0 && (
        <div style={{ padding: 12, fontSize: 11 }} className="muted">
          No deployments found.
        </div>
      )}
      {filtered.length > 0 && (
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
              {HEADERS.map((h) => (
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
              ))}
            </tr>
          </thead>
          <tbody>
            {filtered.map((d, i) => {
              const [ready, desired] = d.ready.split("/").map((n) => parseInt(n, 10));
              const unhealthy = Number.isFinite(desired) && ready < desired;
              return (
                <tr
                  key={`${d.namespace}/${d.name}`}
                  style={{
                    background: i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.012)",
                    borderTop: "1px solid var(--border-weak)",
                  }}
                >
                  <td
                    style={{ padding: "5px 8px", color: "var(--text-muted)", fontSize: 9 }}
                  >
                    {d.namespace}
                  </td>
                  <td style={{ padding: "5px 8px", fontWeight: 500 }}>{d.name}</td>
                  <td
                    style={{
                      padding: "5px 8px",
                      color: unhealthy ? "var(--warning)" : "var(--text-primary)",
                    }}
                  >
                    {d.ready}
                  </td>
                  <td style={{ padding: "5px 8px" }}>{d.up_to_date}</td>
                  <td style={{ padding: "5px 8px" }}>{d.available}</td>
                  <td
                    style={{
                      padding: "5px 8px",
                      color: "var(--text-muted)",
                      fontSize: 9,
                      whiteSpace: "nowrap",
                    }}
                    title={d.age || undefined}
                  >
                    {formatAge(d.age)}
                  </td>
                  <td style={{ padding: "4px 8px" }}>
                    {actions.renderActions(d.namespace, d.name)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {actions.dialogs}
    </div>
  );
}
