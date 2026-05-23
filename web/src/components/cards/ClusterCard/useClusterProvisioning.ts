import { useEffect, useState } from "react";
import type { UseQueryResult } from "@tanstack/react-query";

import { aksApi, tasksApi } from "@/api/endpoints";
import type { CeleryTaskStatus } from "@/api/tasks";
import { formatApiError } from "@/api/client";
import type { AksClusterSummary } from "@/api/endpoints";
import {
  DEFAULT_AKS_SKU,
  DEFAULT_AKS_SYSTEM_SKU,
} from "@/hooks/useAksSkus";

const DEFAULT_NODE_COUNT = 10;
const DEFAULT_SYSTEM_NODE_COUNT = 1;

export const MAX_SYSTEM_NODE_COUNT = 3;
export const CLUSTER_NAME_RE = /^[a-zA-Z][a-zA-Z0-9-]{1,62}$/;

const CLUSTER_NAME_PREFIX = "elb-cluster";
const ELB_CLUSTER_NAME_RE = new RegExp(`^${CLUSTER_NAME_PREFIX}-(\\d+)$`);
const ELB_RG_NAME_RE = new RegExp(`^rg-${CLUSTER_NAME_PREFIX}-(\\d+)$`);

/** Default workload resource group suggested when the modal opens. The
 *  user can edit it to anything that passes `RESOURCE_GROUP_NAME_RE`. */
export const DEFAULT_PROVISION_RESOURCE_GROUP = "rg-elb-cluster";

/** Azure resource group naming rules (Microsoft Learn):
 *  - 1..90 characters
 *  - letters, digits, periods, underscores, hyphens, parentheses
 *  - cannot end with a period
 *  Validated client-side so the Create button stays disabled before the
 *  request reaches ARM. */
export const RESOURCE_GROUP_NAME_RE = /^[A-Za-z0-9._()\-]{1,90}$/;

export function resourceGroupNameValid(name: string): boolean {
  return RESOURCE_GROUP_NAME_RE.test(name) && !name.endsWith(".");
}

/** Walk a regex over a list of names and return the highest captured number,
 *  or 0 if no name matches. Shared by `nextElbClusterName` /
 *  `nextFreeElbIndex`. */
function maxIndexMatching(names: string[], re: RegExp): number {
  let max = 0;
  for (const name of names) {
    const m = re.exec(name);
    if (m) {
      const n = parseInt(m[1], 10);
      if (Number.isFinite(n) && n > max) max = n;
    }
  }
  return max;
}

/** Suggest the next sequential `elb-cluster-NN` name by scanning existing
 *  cluster names *and* resource-group names. We look at both so an orphan
 *  RG left over from a previously deleted cluster doesn't make the default
 *  suggestion conflict on first open. First creation → `elb-cluster-01`. */
export function nextElbClusterName(
  clusters: { name: string }[],
  resourceGroupNames: string[] = [],
): string {
  const fromClusters = maxIndexMatching(
    clusters.map((c) => c.name),
    ELB_CLUSTER_NAME_RE,
  );
  const fromRgs = maxIndexMatching(resourceGroupNames, ELB_RG_NAME_RE);
  const next = Math.max(fromClusters, fromRgs) + 1;
  return `${CLUSTER_NAME_PREFIX}-${String(next).padStart(2, "0")}`;
}

export type ProvisionStatus = "idle" | "creating" | "done" | "error";

type ClustersQueryData = { clusters: AksClusterSummary[] };

/**
 * Owns all provision-form state + the AKS provision call. Tracks elapsed
 * seconds while creating, polls the AKS list faster while creating, and
 * flips to "done" as soon as the named cluster appears in the list.
 */
export function useClusterProvisioning(args: {
  subscriptionId: string;
  resourceGroup: string;
  region?: string;
  acrResourceGroup?: string;
  acrName?: string;
  storageResourceGroup?: string;
  storageAccount?: string;
  defaultSystemSku?: string;
  /** Names of resource groups that already exist in the subscription.
   *  Used to warn the user before they submit a duplicate name. */
  existingResourceGroupNames?: string[];
  closeModal: () => void;
  query: UseQueryResult<ClustersQueryData>;
}) {
  const {
    subscriptionId,
    region,
    acrResourceGroup,
    acrName,
    storageResourceGroup,
    storageAccount,
    defaultSystemSku,
    existingResourceGroupNames,
    closeModal,
    query,
  } = args;
  // `args.resourceGroup` is the dashboard-wide workload RG; the provision
  // modal lets the user override it (see `provisionResourceGroup` below),
  // so it is intentionally not destructured here.

  const [clusterName, setClusterName] = useState("elb-cluster-01");
  const [nodeSku, setNodeSku] = useState(DEFAULT_AKS_SKU);
  const [nodeCount, setNodeCount] = useState(DEFAULT_NODE_COUNT);
  const [systemVmSize, setSystemVmSize] = useState(DEFAULT_AKS_SYSTEM_SKU);
  const [systemNodeCount, setSystemNodeCount] = useState(DEFAULT_SYSTEM_NODE_COUNT);
  // Modal-local overrides so the user can pick a different region / RG for
  // *this* AKS cluster without touching the dashboard-wide selectors at the
  // top of the page. Defaults: region falls back to the dashboard's region;
  // RG starts at DEFAULT_PROVISION_RESOURCE_GROUP regardless of what the
  // dashboard is pointed at (the cluster typically lives in its own folder).
  const [provisionRegion, setProvisionRegionState] = useState<string>(region ?? "");
  // Track whether the user has overridden the region inside the modal so we
  // can keep `provisionRegion` in sync with the dashboard's region picker
  // *only* while the user hasn't touched it.
  const [regionUserTouched, setRegionUserTouched] = useState(false);
  const setProvisionRegion = (value: string) => {
    setRegionUserTouched(true);
    setProvisionRegionState(value);
  };
  useEffect(() => {
    if (!regionUserTouched && region && region !== provisionRegion) {
      setProvisionRegionState(region);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [region]);

  const [provisionResourceGroup, setProvisionResourceGroupState] = useState(
    DEFAULT_PROVISION_RESOURCE_GROUP,
  );
  // Mirror the region pattern: keep RG synced with cluster name while the
  // user hasn't touched the RG field. Once they edit RG directly, their
  // value is locked in and no longer follows cluster-name changes.
  const [rgUserTouched, setRgUserTouched] = useState(false);
  const setProvisionResourceGroup = (value: string) => {
    setRgUserTouched(true);
    setProvisionResourceGroupState(value);
  };
  useEffect(() => {
    if (rgUserTouched) return;
    // Mirror cluster name into RG, but strip the trailing `-NN` sequence
    // so every cluster in the `elb-cluster-NN` family lands in the same
    // `rg-elb-cluster` resource group instead of one RG per index.
    // Custom names without a `-NN` suffix still mirror unchanged
    // (`my-test` → `rg-my-test`).
    if (!CLUSTER_NAME_RE.test(clusterName)) return;
    const baseName = clusterName.replace(/-\d+$/, "");
    const suggested = `rg-${baseName}`;
    if (suggested !== provisionResourceGroup) {
      setProvisionResourceGroupState(suggested);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clusterName]);
  const [provStatus, setProvStatus] = useState<ProvisionStatus>("idle");
  const [provError, setProvError] = useState<string | null>(null);
  const [provStart, setProvStart] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState(0);
  /** Celery task id returned by /api/aks/provision. We poll its status to
   *  detect failures the cluster-list poller can't see (e.g. the worker
   *  failed to reach ARM at all). */
  const [taskId, setTaskId] = useState<string | null>(null);
  /** Latest phase reported by the task (e.g. "creating_cluster",
   *  "arm_create_or_update", "ensuring_rbac"). Surfaced in the banner so the
   *  user can tell live progress from a stuck timer. */
  const [taskPhase, setTaskPhase] = useState<string | null>(null);

  // Adopt the backend's system-pool default the first time it loads.
  useEffect(() => {
    if (defaultSystemSku && systemVmSize === DEFAULT_AKS_SYSTEM_SKU) {
      setSystemVmSize(defaultSystemSku);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultSystemSku]);

  // Tick the elapsed counter every 1 s while creating.
  useEffect(() => {
    if (provStatus !== "creating") return;
    const timer = setInterval(
      () => setElapsed(Math.floor((Date.now() - (provStart ?? Date.now())) / 1000)),
      1000,
    );
    return () => clearInterval(timer);
  }, [provStatus, provStart]);

  // Hard timeout. AKS provisioning normally finishes in 5–10 minutes. If we
  // are still "creating" after 20 minutes and the cluster never appeared in
  // the AKS list, something is wrong (worker died, ARM never got the call,
  // network blocked, RBAC denied the task before the ARM PUT, ...). Surface
  // it so the user stops staring at a ghost timer.
  useEffect(() => {
    if (provStatus !== "creating") return;
    if (elapsed < 20 * 60) return;
    setProvStatus("error");
    setProvError(
      "Provisioning timed out after 20 minutes. The cluster never appeared in the AKS list. " +
      "Check the Azure portal, the worker sidecar logs, and (for local dev) that your `az login` " +
      "identity has Contributor on the target resource group.",
    );
  }, [provStatus, elapsed]);

  // Poll the Celery task itself so we hear about failures the cluster-list
  // poller can't see. Until 2026-05 the only failure path was the modal
  // catch above (POST itself failing) — if the POST succeeded but the
  // worker later crashed (storage AuthFailed, ARM 403, code bug), the FE
  // would sit at "Provisioning..." forever. Polling /api/tasks/{id}
  // gives us authoritative SUCCESS / FAILURE / REVOKED.
  useEffect(() => {
    if (provStatus !== "creating" || !taskId) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await tasksApi.status(taskId);
        if (cancelled) return;
        const phase = (res.progress?.phase as string | undefined) ?? null;
        if (phase) setTaskPhase(phase);
        const status: CeleryTaskStatus = res.status;
        if (status === "FAILURE" || status === "REVOKED") {
          setProvStatus("error");
          setProvError(
            res.error?.trim()
              ? `Provisioning task failed: ${res.error}`
              : status === "REVOKED"
                ? "Provisioning task was cancelled before it finished."
                : "Provisioning task failed without an error message. Check worker logs.",
          );
        }
      } catch {
        // Transient — swallow one poll error so a 500 doesn't kill the
        // banner. The hard timeout above will still catch us if the task
        // is genuinely gone.
      }
    };
    void poll();
    const timer = setInterval(poll, 5_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [provStatus, taskId]);

  // Auto-dismiss provStatus after 10 s
  useEffect(() => {
    if (provStatus !== "done") return;
    const t = setTimeout(() => setProvStatus("idle"), 10_000);
    return () => clearTimeout(t);
  }, [provStatus]);

  // While creating, poll the AKS list faster (10 s) to detect the new cluster.
  useEffect(() => {
    if (provStatus !== "creating") return;
    const t = setInterval(() => query.refetch(), 10_000);
    return () => clearInterval(t);
  }, [provStatus, query]);

  // Flip to "done" the moment the named cluster appears in the list.
  useEffect(() => {
    if (provStatus !== "creating" || !query.data?.clusters) return;
    const found = query.data.clusters.find((c) => c.name === clusterName);
    if (found) {
      setProvStatus("done");
    }
  }, [provStatus, query.data, clusterName]);

  const handleProvision = async () => {
    if (!provisionRegion) return;
    if (!resourceGroupNameValid(provisionResourceGroup)) return;
    // Defense in depth: the Create button is also disabled on conflict, but
    // a programmatic invocation (keyboard, future code path) must not slip
    // a duplicate RG into ARM.
    const conflict = (existingResourceGroupNames ?? []).some(
      (n) => n.toLowerCase() === provisionResourceGroup.toLowerCase(),
    );
    if (conflict) return;
    setProvStatus("creating");
    setProvError(null);
    setProvStart(Date.now());
    setTaskId(null);
    setTaskPhase(null);
    // Do NOT close the modal here. If the ARM request fails (auth, quota,
    // RegionNotAllowed, …) the user would lose every field they typed and
    // only see an error banner outside. Close only after the API accepts
    // the request — by that point a "provisioning" record exists and the
    // banner can take over.
    try {
      const response = await aksApi.provision({
        subscription_id: subscriptionId,
        resource_group: provisionResourceGroup,
        region: provisionRegion,
        cluster_name: clusterName,
        node_sku: nodeSku,
        node_count: nodeCount,
        // Sibling repo's two-pool layout (constants.py):
        //   systempool (mode=System, CriticalAddonsOnly taint)
        //   blastpool  (mode=User, workload=blast taint)
        system_vm_size: systemVmSize,
        system_node_count: systemNodeCount,
        acr_resource_group: acrResourceGroup || "",
        acr_name: acrName || "",
        storage_resource_group: storageResourceGroup || provisionResourceGroup,
        storage_account: storageAccount || "",
      });
      // Capture the Celery task id so the poller above can drive the
      // banner. The api route returns several aliases (task_id /
      // instance_id / id) — pick whichever is present.
      const tid =
        response?.task_id ?? response?.instance_id ?? response?.id ?? null;
      if (tid) setTaskId(tid);
      closeModal();
    } catch (e) {
      setProvError(formatApiError(e, "aks"));
      setProvStatus("error");
      // Modal intentionally stays open so the user can see the error in
      // the sticky footer and either fix the form or click Cancel.
    }
  };

  // Reset region to whatever the dashboard's region picker currently holds
  // and clear the userTouched flag. Called by the parent when (re)opening
  // the provision modal so each open starts from a known state.
  const resetProvisionRegionToDashboard = () => {
    setRegionUserTouched(false);
    setProvisionRegionState(region ?? "");
  };

  // Clear the RG userTouched flag so the auto-sync useEffect resumes
  // tracking cluster-name changes. The actual RG value is populated by
  // that effect once a valid cluster name is set.
  const resetProvisionResourceGroupTracking = () => {
    setRgUserTouched(false);
  };

  const clusterNameValid = CLUSTER_NAME_RE.test(clusterName);

  const provisionResourceGroupValid = resourceGroupNameValid(provisionResourceGroup);
  // Conflict = an existing resource group already uses this exact name.
  // Case-insensitive because Azure RG names are case-insensitive.
  const provisionResourceGroupConflict = (existingResourceGroupNames ?? []).some(
    (n) => n.toLowerCase() === provisionResourceGroup.toLowerCase(),
  );

  return {
    // form state
    clusterName,
    setClusterName,
    nodeSku,
    setNodeSku,
    nodeCount,
    setNodeCount,
    systemVmSize,
    setSystemVmSize,
    systemNodeCount,
    setSystemNodeCount,
    provisionRegion,
    setProvisionRegion,
    resetProvisionRegionToDashboard,
    provisionResourceGroup,
    setProvisionResourceGroup,
    resetProvisionResourceGroupTracking,
    provisionResourceGroupValid,
    provisionResourceGroupConflict,
    // status
    provStatus,
    setProvStatus,
    provError,
    setProvError,
    elapsed,
    /** Phase reported by the Celery task; null while we wait for the first
     *  poll to come back. Banner shows this so a stuck task looks stuck. */
    taskPhase,
    clusterNameValid,
    handleProvision,
  };
}
