import { useCallback, useState } from "react";
import { FileText, Terminal, Trash2 } from "lucide-react";

import { formatApiError } from "@/api/client";
import { ConfirmDialog } from "@/components/ConfirmDialog";

import { PodDescribeDialog } from "./PodDescribeDialog";
import { PodLogsDialog } from "./PodLogsDialog";

/**
 * Shared per-row action lifecycle (Logs / Describe / Delete) for the cluster
 * Workloads tabs. The Pods, Deployments and Jobs panels each own a different
 * data shape but their row actions are identical in behaviour, so this hook
 * centralises the dialog state, the confirm/delete flow and the
 * system-namespace gate. The backend route is the authoritative gate
 * (returns 403 for system namespaces); the SPA-side `SYSTEM_NAMESPACES`
 * check below only hides the button as a convenience.
 *
 * Responsibility: own the Logs/Describe/Delete UI state for one workload kind
 *   and expose a row-actions renderer plus the (single) dialog stack.
 * Edit boundaries: presentation + client state only; never call Azure/K8s
 *   directly — callers inject typed `monitoringApi` functions.
 * Key entry points: `useWorkloadActions`, returned `renderActions` / `dialogs`.
 * Risky contracts: delete return shape `{ status, ... }`; system-namespace
 *   gate must stay aligned with the backend `SYSTEM_NAMESPACES` refusal.
 * Validation: cd web && npm run build; exercised by the panel components.
 */

export const SYSTEM_NAMESPACES = new Set([
  "kube-system",
  "kube-public",
  "kube-node-lease",
  "gatekeeper-system",
  "azure-arc",
  "calico-system",
  "tigera-operator",
]);

// Compact square icon-only button. Square padding keeps the three actions
// visually balanced and matches the dense table row height.
const iconButtonStyle: React.CSSProperties = {
  padding: "4px 6px",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  lineHeight: 0,
};

interface DeleteResult {
  status: string;
}

export interface WorkloadActionApi {
  /** Fetch the last `tail` lines of logs (a representative pod for
   * Deployments / Jobs). The backend `_graceful` path returns
   * `logs: ""` plus `degraded` / `degraded_reason` when the pod log GET
   * fails (commonly: the Job's Spot node was reclaimed, so the pod object
   * lingers but its log is no longer readable). */
  logs: (
    namespace: string,
    name: string,
  ) => Promise<{ logs: string; degraded?: boolean; degraded_reason?: string }>;
  describe: (namespace: string, name: string) => Promise<{ describe: string }>;
  del: (namespace: string, name: string) => Promise<DeleteResult>;
}

/** Human explanation for an empty/degraded log fetch so the dialog never
 *  shows a silent `(empty)`. `kind` lets the Job case name the most common
 *  real cause (Spot node reclaimed after the Job finished). The
 *  `degraded_reason` codes come from `_classify_exception` in
 *  `api/routes/monitor/common.py`. */
function degradedLogMessage(kind: string, reason: string | undefined): string {
  const lower = kind.toLowerCase();
  const suffix = reason ? ` (reason: ${reason})` : "";
  if (lower === "job") {
    return (
      `Logs are no longer available${suffix}. The Job has finished and its ` +
      `node was likely reclaimed (BLAST search Jobs run on Azure Spot nodes), ` +
      `so the on-cluster pod log is gone. The BLAST results were already ` +
      `shipped to Storage — view them from the job's results instead.`
    );
  }
  return (
    `Logs could not be fetched${suffix}. The pod may have been removed or its ` +
    `node reclaimed, so the on-cluster log is no longer readable.`
  );
}

interface Target {
  namespace: string;
  name: string;
}

/**
 * @param kind Display label ("Pod" / "Deployment" / "Job").
 * @param api Injected typed client calls for the kind.
 * @param onDeleted Invoked after a successful delete so the parent can
 *   refetch the list (the backend already invalidated its snapshot cache).
 * @param deleteCopy Optional confirm-dialog copy overrides.
 */
export function useWorkloadActions(
  kind: string,
  api: WorkloadActionApi,
  onDeleted: () => void,
  deleteCopy?: { details?: string[]; footnote?: string },
) {
  const lower = kind.toLowerCase();

  const [logTarget, setLogTarget] = useState<Target | null>(null);
  const [logOutput, setLogOutput] = useState<string | null>(null);
  const [logLoading, setLogLoading] = useState(false);

  const [describeTarget, setDescribeTarget] = useState<Target | null>(null);
  const [describeOutput, setDescribeOutput] = useState<string | null>(null);
  const [describeLoading, setDescribeLoading] = useState(false);

  const [pendingDelete, setPendingDelete] = useState<Target | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const fetchLogs = useCallback(
    async (namespace: string, name: string) => {
      setLogTarget({ namespace, name });
      setLogOutput(null);
      setLogLoading(true);
      try {
        const r = await api.logs(namespace, name);
        if (r.logs) {
          setLogOutput(r.logs);
        } else if (r.degraded) {
          // Backend hit an exception fetching the pod log and degraded to
          // an empty string. Surface the reason instead of a blank
          // `(empty)` (issue #28).
          setLogOutput(degradedLogMessage(kind, r.degraded_reason));
        } else {
          setLogOutput("(empty)");
        }
      } catch (e) {
        setLogOutput(`Error: ${(e as Error).message}`);
      } finally {
        setLogLoading(false);
      }
    },
    [api, kind],
  );
  const closeLogs = () => {
    setLogTarget(null);
    setLogOutput(null);
  };

  const fetchDescribe = useCallback(
    async (namespace: string, name: string) => {
      setDescribeTarget({ namespace, name });
      setDescribeOutput(null);
      setDescribeLoading(true);
      try {
        const r = await api.describe(namespace, name);
        setDescribeOutput(r.describe || "(empty)");
      } catch (e) {
        setDescribeOutput(`Error: ${(e as Error).message}`);
      } finally {
        setDescribeLoading(false);
      }
    },
    [api],
  );
  const closeDescribe = () => {
    setDescribeTarget(null);
    setDescribeOutput(null);
  };

  const requestDelete = useCallback((namespace: string, name: string) => {
    setDeleteError(null);
    setPendingDelete({ namespace, name });
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
      await api.del(pendingDelete.namespace, pendingDelete.name);
      setPendingDelete(null);
      onDeleted();
    } catch (e) {
      setDeleteError(formatApiError(e, "aks"));
    } finally {
      setDeleting(false);
    }
  }, [pendingDelete, deleting, api, onDeleted]);

  const renderActions = (namespace: string, name: string) => (
    <div style={{ display: "inline-flex", gap: 4 }}>
      <button
        className="glass-button k8s-pods-logs-button"
        onClick={() => fetchLogs(namespace, name)}
        style={iconButtonStyle}
        title={`Logs: ${name}`}
        aria-label={`View logs for ${lower} ${name}`}
      >
        <Terminal size={12} strokeWidth={1.5} />
      </button>
      <button
        className="glass-button k8s-pods-describe-button"
        onClick={() => fetchDescribe(namespace, name)}
        style={iconButtonStyle}
        title={`Describe: ${name}`}
        aria-label={`Describe ${lower} ${name}`}
      >
        <FileText size={12} strokeWidth={1.5} />
      </button>
      {!SYSTEM_NAMESPACES.has(namespace) && (
        <button
          className="glass-button glass-button--danger k8s-pods-delete-button"
          onClick={() => requestDelete(namespace, name)}
          style={iconButtonStyle}
          title={`Delete ${lower}: ${name}`}
          aria-label={`Delete ${lower} ${name}`}
        >
          <Trash2 size={12} strokeWidth={1.5} />
        </button>
      )}
    </div>
  );

  const dialogs = (
    <>
      {logTarget && (
        <PodLogsDialog
          target={logTarget}
          kind={kind}
          output={logOutput}
          loading={logLoading}
          onRefresh={() => fetchLogs(logTarget.namespace, logTarget.name)}
          onClose={closeLogs}
        />
      )}
      {describeTarget && (
        <PodDescribeDialog
          target={describeTarget}
          kind={kind}
          output={describeOutput}
          loading={describeLoading}
          onRefresh={() => fetchDescribe(describeTarget.namespace, describeTarget.name)}
          onClose={closeDescribe}
        />
      )}
      {pendingDelete && (
        <ConfirmDialog
          title={`Delete ${lower}?`}
          message={`${pendingDelete.namespace} / ${pendingDelete.name}`}
          details={deleteCopy?.details}
          footnote={
            deleteError
              ? `Last attempt failed: ${deleteError}`
              : deleteCopy?.footnote
          }
          confirmLabel={deleting ? "Deleting…" : "Delete"}
          tone="danger"
          onConfirm={performDelete}
          onCancel={cancelDelete}
        />
      )}
    </>
  );

  return { renderActions, dialogs };
}
