import { Loader2 } from "lucide-react";

import { monitoringApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import type { K8sJob } from "@/api/endpoints";

import { formatAge, formatDuration } from "./k8sFormat";
import { NamespaceFilter } from "./NamespaceFilter";
import { useAgeSortedInfinite } from "./useAgeSortedInfinite";
import { useNamespaceFilter } from "./useNamespaceFilter";
import { useWorkloadActions } from "./useWorkloadActions";
import { WorkloadScroll } from "./WorkloadScroll";

/**
 * Jobs tab of the cluster Workloads card. Table mirroring the Azure portal
 * Jobs view (Completions / Status / Duration / Age) with the same namespace
 * filter as the other tabs, plus the shared per-row Logs / Describe / Delete
 * actions (`useWorkloadActions`). This is where finished and in-flight
 * ElasticBLAST search Jobs surface; Delete removes the Job (and its pods via
 * Background propagation). The collapse chrome lives in the parent
 * `K8sWorkloadsSection`.
 */
export interface K8sJobsQuery {
  isLoading: boolean;
  isFetching?: boolean;
  isError: boolean;
  data?: { jobs: K8sJob[] } | null;
  error?: unknown;
  refetch?: () => void;
}

const HEADERS = ["NS", "NAME", "COMPLETIONS", "STATUS", "DURATION", "AGE", ""];

function statusColor(status: string): string {
  const v = status.toLowerCase();
  if (v === "complete") return "var(--success)";
  if (v === "failed") return "var(--danger)";
  if (v === "running") return "var(--accent)";
  return "var(--warning)";
}

export function K8sJobsPanel({
  query,
  subscriptionId,
  resourceGroup,
  clusterName,
}: {
  query: K8sJobsQuery;
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
}) {
  const all = query.data?.jobs ?? [];
  const { effectiveNs, setNsFilter, namespaces, filtered } = useNamespaceFilter(all);
  // Newest-first, rendered 20 rows at a time with infinite scroll so a
  // 1000+ job roster does not paint every row up front.
  const { visible, hasMore, total, scrollRef, sentinelRef } =
    useAgeSortedInfinite(filtered);

  const actions = useWorkloadActions(
    "Job",
    {
      logs: (ns, name) =>
        monitoringApi.k8sJobLogs(subscriptionId, resourceGroup, clusterName, ns, name, 200),
      describe: (ns, name) =>
        monitoringApi.k8sJobDescribe(subscriptionId, resourceGroup, clusterName, ns, name),
      del: (ns, name) =>
        monitoringApi.k8sJobDelete(subscriptionId, resourceGroup, clusterName, ns, name),
    },
    () => query.refetch?.(),
    {
      details: [
        "The Job and all pods it created will be deleted.",
        "A running Job will be stopped; completed results already written off-cluster are unaffected.",
      ],
      footnote: "Logs from the Job's pods will be lost unless already shipped off-cluster.",
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
        idSuffix="jobs"
        items={all}
        namespaces={namespaces}
        value={effectiveNs}
        onChange={setNsFilter}
        shown={total}
      />
      {!query.isLoading && !query.isError && all.length === 0 && (
        <div style={{ padding: 12, fontSize: 11 }} className="muted">
          No jobs found.
        </div>
      )}
      {total > 0 && (
        <WorkloadScroll
          scrollRef={scrollRef}
          sentinelRef={sentinelRef}
          shown={visible.length}
          total={total}
          hasMore={hasMore}
        >
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
            {visible.map((j, i) => (
              <tr
                key={`${j.namespace}/${j.name}`}
                style={{
                  background: i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.012)",
                  borderTop: "1px solid var(--border-weak)",
                }}
              >
                <td
                  style={{ padding: "5px 8px", color: "var(--text-muted)", fontSize: 9 }}
                >
                  {j.namespace}
                </td>
                <td style={{ padding: "5px 8px", fontWeight: 500 }}>{j.name}</td>
                <td style={{ padding: "5px 8px" }}>{j.completions}</td>
                <td style={{ padding: "5px 8px", color: statusColor(j.status) }}>
                  <span
                    style={{
                      display: "inline-block",
                      width: 5,
                      height: 5,
                      borderRadius: "50%",
                      background: statusColor(j.status),
                      marginRight: 4,
                      verticalAlign: "middle",
                    }}
                  />
                  {j.status}
                </td>
                <td
                  style={{
                    padding: "5px 8px",
                    color: "var(--text-muted)",
                    fontSize: 9,
                    whiteSpace: "nowrap",
                  }}
                >
                  {formatDuration(j.start_time, j.completion_time)}
                </td>
                <td
                  style={{
                    padding: "5px 8px",
                    color: "var(--text-muted)",
                    fontSize: 9,
                    whiteSpace: "nowrap",
                  }}
                  title={j.age || undefined}
                >
                  {formatAge(j.age)}
                </td>
                <td style={{ padding: "4px 8px" }}>
                  {actions.renderActions(j.namespace, j.name)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </WorkloadScroll>
      )}
      {actions.dialogs}
    </div>
  );
}
