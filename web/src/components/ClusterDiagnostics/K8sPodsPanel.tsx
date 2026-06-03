import { useCallback, useState } from "react";
import { FileText, Loader2, Terminal, Trash2 } from "lucide-react";

import { monitoringApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import type { K8sPod } from "@/api/endpoints";
import { ConfirmDialog } from "@/components/ConfirmDialog";

import { formatAge } from "./k8sFormat";
import { NamespaceFilter } from "./NamespaceFilter";
import { PodDescribeDialog } from "./PodDescribeDialog";
import { PodLogsDialog } from "./PodLogsDialog";
import { useNamespaceFilter } from "./useNamespaceFilter";

/**
 * Pods tab of the cluster Workloads card. Lists pods of every phase
 * (Pending, Running, Succeeded/Completed, Failed) across all namespaces —
 * mirroring the Azure portal Pods view — with a namespace filter and
 * Node / Pod IP columns. Owns the per-row action lifecycle (Logs / Describe
 * / Delete). The Delete action gates on system-managed namespaces both here
 * (button hidden) and on the backend route (`/api/monitor/aks/pod` returns
 * 403); the SPA-side gate is convenience, the route is authoritative. The
 * collapse chrome lives in the parent `K8sWorkloadsSection`.
 */
const SYSTEM_NAMESPACES = new Set([
  "kube-system",
  "kube-public",
  "kube-node-lease",
  "gatekeeper-system",
  "azure-arc",
  "calico-system",
  "tigera-operator",
]);

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
  const [logTarget, setLogTarget] = useState<{ namespace: string; pod: string } | null>(
    null,
  );
  const [logOutput, setLogOutput] = useState<string | null>(null);
  const [logLoading, setLogLoading] = useState(false);
  const [describeTarget, setDescribeTarget] = useState<{
    namespace: string;
    pod: string;
  } | null>(null);
  const [describeOutput, setDescribeOutput] = useState<string | null>(null);
  const [describeLoading, setDescribeLoading] = useState(false);
  // Confirm dialog target. `pendingDelete` is null when the dialog is closed;
  // setting it opens the dialog. `deleting` flips during the in-flight DELETE
  // so we can disable the confirm button and avoid double-submits.
  const [pendingDelete, setPendingDelete] = useState<{
    namespace: string;
    pod: string;
  } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const allPods = query.data?.pods ?? [];
  const { effectiveNs, setNsFilter, namespaces, filtered: pods } =
    useNamespaceFilter(allPods);

  const sc = (s: string) => {
    const v = s.toLowerCase();
    return v === "running"
      ? "var(--success)"
      : v.includes("error") || v.includes("crash")
        ? "var(--danger)"
        : "var(--warning)";
  };
  const fetchLogs = useCallback(
    async (ns: string, pod: string) => {
      setLogTarget({ namespace: ns, pod });
      setLogOutput(null);
      setLogLoading(true);
      try {
        const r = await monitoringApi.k8sPodLogs(
          subscriptionId,
          resourceGroup,
          clusterName,
          ns,
          pod,
          200,
        );
        setLogOutput(r.logs || "(empty)");
      } catch (e) {
        setLogOutput(`Error: ${(e as Error).message}`);
      } finally {
        setLogLoading(false);
      }
    },
    [subscriptionId, resourceGroup, clusterName],
  );
  const closeLogs = () => {
    setLogTarget(null);
    setLogOutput(null);
  };
  const fetchDescribe = useCallback(
    async (ns: string, pod: string) => {
      setDescribeTarget({ namespace: ns, pod });
      setDescribeOutput(null);
      setDescribeLoading(true);
      try {
        const r = await monitoringApi.k8sPodDescribe(
          subscriptionId,
          resourceGroup,
          clusterName,
          ns,
          pod,
        );
        setDescribeOutput(r.describe || "(empty)");
      } catch (e) {
        setDescribeOutput(`Error: ${(e as Error).message}`);
      } finally {
        setDescribeLoading(false);
      }
    },
    [subscriptionId, resourceGroup, clusterName],
  );
  const closeDescribe = () => {
    setDescribeTarget(null);
    setDescribeOutput(null);
  };
  const requestDelete = useCallback((ns: string, pod: string) => {
    setDeleteError(null);
    setPendingDelete({ namespace: ns, pod });
  }, []);
  const cancelDelete = useCallback(() => {
    if (deleting) return;
    setPendingDelete(null);
    setDeleteError(null);
  }, [deleting]);
  const performDelete = useCallback(async () => {
    if (!pendingDelete || deleting) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await monitoringApi.k8sPodDelete(
        subscriptionId,
        resourceGroup,
        clusterName,
        pendingDelete.namespace,
        pendingDelete.pod,
      );
      setPendingDelete(null);
      // Surface the deletion immediately. The backend already invalidated
      // the pods snapshot cache so the next refetch returns fresh state.
      query.refetch?.();
    } catch (e) {
      setDeleteError(formatApiError(e, "aks"));
    } finally {
      setDeleting(false);
    }
  }, [pendingDelete, deleting, subscriptionId, resourceGroup, clusterName, query]);

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
                  <div style={{ display: "inline-flex", gap: 4 }}>
                    <button
                      className="glass-button k8s-pods-logs-button"
                      onClick={() => fetchLogs(p.namespace, p.name)}
                      style={iconButtonStyle}
                      title={`Logs: ${p.name}`}
                      aria-label={`View logs for pod ${p.name}`}
                    >
                      <Terminal size={12} strokeWidth={1.5} />
                    </button>
                    <button
                      className="glass-button k8s-pods-describe-button"
                      onClick={() => fetchDescribe(p.namespace, p.name)}
                      style={iconButtonStyle}
                      title={`Describe: ${p.name}`}
                      aria-label={`Describe pod ${p.name}`}
                    >
                      <FileText size={12} strokeWidth={1.5} />
                    </button>
                    {!SYSTEM_NAMESPACES.has(p.namespace) && (
                      <button
                        className="glass-button glass-button--danger k8s-pods-delete-button"
                        onClick={() => requestDelete(p.namespace, p.name)}
                        style={iconButtonStyle}
                        title={`Delete pod: ${p.name}`}
                        aria-label={`Delete pod ${p.name}`}
                      >
                        <Trash2 size={12} strokeWidth={1.5} />
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {logTarget && (
        <PodLogsDialog
          target={logTarget}
          output={logOutput}
          loading={logLoading}
          onRefresh={() => fetchLogs(logTarget.namespace, logTarget.pod)}
          onClose={closeLogs}
        />
      )}
      {describeTarget && (
        <PodDescribeDialog
          target={describeTarget}
          output={describeOutput}
          loading={describeLoading}
          onRefresh={() => fetchDescribe(describeTarget.namespace, describeTarget.pod)}
          onClose={closeDescribe}
        />
      )}
      {pendingDelete && (
        <ConfirmDialog
          title="Delete pod?"
          message={`${pendingDelete.namespace} / ${pendingDelete.pod}`}
          details={[
            "A controller (Deployment / ReplicaSet / StatefulSet) may recreate it on its own — this is normal Kubernetes behaviour.",
            "Pods backed by a Job will not restart; the Job will be marked failed if no other pods complete the work.",
          ]}
          footnote={
            deleteError
              ? `Last attempt failed: ${deleteError}`
              : "Logs from this pod will be lost unless they have already been shipped off-cluster."
          }
          confirmLabel={deleting ? "Deleting…" : "Delete"}
          tone="danger"
          onConfirm={performDelete}
          onCancel={cancelDelete}
        />
      )}
    </div>
  );
}

// Compact square icon-only button. Square padding keeps the three actions
// visually balanced and matches the dense table row height.
const iconButtonStyle: React.CSSProperties = {
  padding: "4px 6px",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  lineHeight: 0,
};
