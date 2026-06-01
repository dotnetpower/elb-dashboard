import { useEffect, useState } from "react";
import type { UseQueryResult } from "@tanstack/react-query";

import { aksApi, tasksApi } from "@/api/endpoints";
import type { AksPreflightResponse, CeleryTaskStatus } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import { reportClientError } from "@/api/clientLog";
import type { AksClusterSummary } from "@/api/endpoints";
import {
  DEFAULT_AKS_SKU,
  DEFAULT_AKS_SYSTEM_NODE_COUNT,
  DEFAULT_AKS_SYSTEM_SKU,
} from "@/hooks/useAksSkus";

const DEFAULT_NODE_COUNT = 10;

export const MAX_SYSTEM_NODE_COUNT = 3;
export const CLUSTER_NAME_RE = /^[a-zA-Z][a-zA-Z0-9-]{1,62}$/;

/** Free-form cluster classification label written to the `elb-tier` ARM tag.
 *  The dashboard's cluster card surfaces this as a chip so a multi-cluster
 *  deployment ("heavy" / "light") stays readable at a glance.
 *  Empty string == leave the tag off entirely. */
export type ClusterTier = "" | "heavy" | "light" | "general";

/** Workload-pool preset applied when the user picks a tier and has not
 *  yet manually overridden the SKU or node count. Picking a tier also
 *  writes the `elb-tier` ARM tag (see `handleProvision`). */
export const CLUSTER_TIER_PRESETS: Record<
  Exclude<ClusterTier, "">,
  { sku: string; nodes: number }
> = {
  light: { sku: "Standard_D16s_v3", nodes: 2 },
  general: { sku: "Standard_E16s_v5", nodes: 5 },
  heavy: { sku: "Standard_E32s_v5", nodes: 10 },
};

export const CLUSTER_TIER_OPTIONS: { value: ClusterTier; label: string }[] = [
  { value: "", label: "(unspecified)" },
  { value: "light", label: "light — D16s_v3 × 2 (quick smoke / dev)" },
  { value: "general", label: "general — E16s_v5 × 5 (mixed workloads)" },
  { value: "heavy", label: "heavy — E32s_v5 × 10 (large BLAST jobs)" },
];

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
  /** Whether the provision modal is currently mounted. The live
   *  preflight effect (P2-1) is gated on this so we do not hammer
   *  `/api/aks/preflight` while the modal is closed. */
  modalOpen?: boolean;
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
    modalOpen = false,
    query,
  } = args;
  // `args.resourceGroup` is the dashboard-wide workload RG; the provision
  // modal lets the user override it (see `provisionResourceGroup` below),
  // so it is intentionally not destructured here.

  const [clusterName, setClusterName] = useState("elb-cluster-01");
  const [nodeSku, setNodeSkuState] = useState(DEFAULT_AKS_SKU);
  // Node count is fully user-controlled. The preflight quota check surfaces
  // `max_blast_nodes_fit` in the modal as a warning with an explicit
  // "Apply N nodes" button — we never mutate the input on the user's behalf.
  const [nodeCount, setNodeCountState] = useState(DEFAULT_NODE_COUNT);
  const [systemVmSize, setSystemVmSize] = useState(DEFAULT_AKS_SYSTEM_SKU);
  const [systemNodeCount, setSystemNodeCount] = useState(DEFAULT_AKS_SYSTEM_NODE_COUNT);
  // Tier presets fill nodeSku/nodeCount only while the user has not yet
  // edited them directly. Any external call to `setNodeSku` / `setNodeCount`
  // (modal SKU dropdown, count input, "Apply N nodes" button) flips the
  // matching flag so subsequent tier changes leave the user's pick alone.
  const [nodeSkuUserTouched, setNodeSkuUserTouched] = useState(false);
  const [nodeCountUserTouched, setNodeCountUserTouched] = useState(false);
  const setNodeSku = (value: string) => {
    setNodeSkuUserTouched(true);
    setNodeSkuState(value);
  };
  const setNodeCount = (value: number) => {
    setNodeCountUserTouched(true);
    setNodeCountState(value);
  };
  /** Free-form cluster classification — written to the `elb-tier` ARM tag
   *  so the dashboard can group multi-cluster deployments. Empty string =
   *  leave the tag off. The picker is optional in the UI. Selecting a
   *  non-empty tier also applies its workload-pool preset to nodeSku /
   *  nodeCount unless the user has already overridden either field. */
  const [tier, setTierState] = useState<ClusterTier>("");
  const setTier = (value: ClusterTier) => {
    setTierState(value);
    if (!value) return;
    const preset = CLUSTER_TIER_PRESETS[value];
    if (!preset) return;
    if (!nodeSkuUserTouched) setNodeSkuState(preset.sku);
    if (!nodeCountUserTouched) setNodeCountState(preset.nodes);
  };
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
  /** Richer progress payload published by `provision_aks` via
   *  `task.update_state(meta=…)`. Carries `step`/`total_steps`,
   *  `message`, `cluster_state`, `pools[]`, `arm_elapsed_seconds`,
   *  `rg_visibility_attempt`/`rg_visibility_total`, etc. The banner
   *  reads whatever keys are present; anything missing degrades to the
   *  basic "phase + elapsed" UX. */
  const [taskProgress, setTaskProgress] = useState<Record<string, unknown> | null>(null);
  /** "idle" before the user hits Create on the current input set;
   *  "checking" while `POST /api/aks/preflight` is in flight;
   *  "done" once results are in. Reset to "idle" whenever inputs that
   *  affect the result change (region, RG, SKUs, counts). */
  const [preflightStatus, setPreflightStatus] = useState<
    "idle" | "checking" | "done"
  >("idle");
  const [preflightResult, setPreflightResult] =
    useState<AksPreflightResponse | null>(null);

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

  // Any input change that affects pre-flight result must invalidate the
  // cached pre-flight so the user has to re-run it before Create. This
  // prevents the "I fixed the SKU but the modal still shows the old
  // failure" footgun. We also kick off an *auto* preflight 500 ms after
  // the user stops typing/clicking so they get live feedback while
  // tuning node count etc., instead of having to keep clicking Create.
  useEffect(() => {
    if (preflightStatus === "idle" && !preflightResult) return;
    setPreflightStatus("idle");
    setPreflightResult(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    provisionRegion,
    provisionResourceGroup,
    nodeSku,
    nodeCount,
    systemVmSize,
    systemNodeCount,
  ]);

  // Live (debounced) preflight: any time the form has the minimum
  // fields filled and the modal is in an editable state, kick off a
  // preflight in the background so the check list stays accurate as
  // the user adjusts node count etc. The debounce keeps us from
  // hammering `/api/aks/preflight` on every keystroke. We skip while
  // provisioning is in flight to avoid masking the live progress
  // panel with a "checking…" indicator.
  useEffect(() => {
    if (!modalOpen) return;
    if (provStatus === "creating") return;
    if (!subscriptionId || !provisionRegion || !clusterName) return;
    if (!resourceGroupNameValid(provisionResourceGroup)) return;
    if (preflightStatus === "checking") return;
    if (preflightResult) return; // already have a fresh result
    const timer = window.setTimeout(() => {
      void (async () => {
        setPreflightStatus("checking");
        try {
          const res = await aksApi.preflight({
            subscription_id: subscriptionId,
            resource_group: provisionResourceGroup,
            region: provisionRegion,
            cluster_name: clusterName,
            node_sku: nodeSku,
            node_count: nodeCount,
            system_vm_size: systemVmSize,
            system_node_count: systemNodeCount,
            acr_resource_group: acrResourceGroup || "",
            acr_name: acrName || "",
            storage_resource_group: storageResourceGroup || "",
            storage_account: storageAccount || "",
          });
          setPreflightResult(res);
        } catch {
          // Live preflight failure is silent — the Create flow will
          // re-run preflight inline and surface any real outage there.
        } finally {
          setPreflightStatus("done");
        }
      })();
    }, 500);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    provStatus,
    modalOpen,
    subscriptionId,
    provisionRegion,
    provisionResourceGroup,
    // `clusterName` is intentionally **excluded** — it has no effect on
    // SKU / quota / RG checks (those only use sub + region + SKUs + RG),
    // so retyping the name should not re-trigger preflight on every
    // keystroke.
    nodeSku,
    nodeCount,
    systemVmSize,
    systemNodeCount,
    // ACR / Storage are inputs to the new `rbac_runtime` preflight row
    // (UAA coverage check). If the dashboard re-renders with different
    // platform resources the auto-preflight must re-run so the row
    // reflects the current targets, not the stale ones.
    acrResourceGroup,
    acrName,
    storageResourceGroup,
    storageAccount,
    preflightStatus,
    preflightResult,
  ]);

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
        const progress = (res.progress ?? null) as Record<string, unknown> | null;
        const phase = (progress?.phase as string | undefined) ?? null;
        if (phase) setTaskPhase(phase);
        if (progress) setTaskProgress(progress);
        const status: CeleryTaskStatus = res.status;
        if (status === "FAILURE" || status === "REVOKED") {
          setProvStatus("error");
          const errMsg = res.error?.trim()
            ? `Provisioning task failed: ${res.error}`
            : status === "REVOKED"
              ? "Provisioning task was cancelled before it finished."
              : "Provisioning task failed without an error message. Check worker logs.";
          setProvError(errMsg);
          // Provisioning errors are transient by design — they live in
          // `provError` only and disappear on browser refresh / Dismiss.
          // Re-introducing localStorage or server hydration here would
          // bring back the stale "Last attempt failed" banner that
          // re-surfaced after a clean cluster delete.
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
    // `clusterName`, `provisionRegion`, `provisionResourceGroup`, and
    // `subscriptionId` are stable for the lifetime of a single "creating"
    // transition (the modal disables those inputs once the task is
    // queued), so adding them to the dep array is safe and silences
    // react-hooks/exhaustive-deps without changing runtime behaviour.
  }, [
    provStatus,
    taskId,
    clusterName,
    provisionRegion,
    provisionResourceGroup,
    subscriptionId,
  ]);

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

  // Two-stage state transition while creating:
  //   1) As soon as the provision task is *accepted* (`taskId` present,
  //      i.e. the enqueue POST returned), close the modal so the user
  //      watches the in-card progress banner — which is already showing
  //      the same Step N/5 / phase / elapsed. We close at enqueue rather
  //      than waiting for ARM to publish `cluster_state` because the
  //      in-card banner duplicates the modal's live panel from that point
  //      on, and the safety net that originally justified keeping the
  //      modal open now lives on the card:
  //        - form inputs persist (this hook stays mounted regardless of
  //          the modal), so an ARM rejection's Retry reopens prefilled;
  //        - the ProvisionErrorCard renders in the card whenever
  //          `provError && !showProvision`, so a task FAILURE / ARM
  //          rejection after the modal closed is still surfaced inline
  //          with Dismiss + Edit-&-retry.
  //      We do NOT mark provStatus "done" here — the "is ready" banner is
  //      reserved for real readiness, otherwise we lie to the user about a
  //      cluster that is still 5–10 minutes from being usable.
  //   2) Only flip to "done" when either (a) the list query returns the
  //      named cluster with `provisioning_state === "Succeeded"`, or
  //      (b) the task itself reports `cluster_state === "Succeeded"`.
  //      While neither holds, provStatus stays "creating" so the list
  //      polling keeps refreshing and the progress banner keeps
  //      rendering.
  useEffect(() => {
    if (provStatus !== "creating") return;
    if (taskId) {
      closeModal();
    }
    const armState = String(taskProgress?.cluster_state ?? "");
    const found = query.data?.clusters?.find((c) => c.name === clusterName);
    const foundSucceeded =
      !!found &&
      String(found.provisioning_state ?? "").toLowerCase() === "succeeded";
    const armSucceeded = armState === "Succeeded";
    if (foundSucceeded || armSucceeded) {
      setProvStatus("done");
      closeModal();
    }
  }, [provStatus, taskId, query.data, clusterName, taskProgress, closeModal]);

  const handleProvision = async () => {
    if (!provisionRegion) return;
    if (!resourceGroupNameValid(provisionResourceGroup)) return;
    // Reusing an existing resource group is intentional: a single RG can
    // host multiple AKS clusters, and `provision_aks` calls
    // `rc.resource_groups.get` idempotently before creating the cluster.
    // We surface the "exists" state purely as an info note in the modal.

    // Two-stage flow: run preflight first when we have no fresh result.
    // The preflight call returns SKU / quota / RG check rows, blocking
    // submit only on `fail` rows. If we already have a passing result
    // from a previous click (no inputs changed since), skip straight to
    // the provision PUT.
    const preflightPayload = {
      subscription_id: subscriptionId,
      resource_group: provisionResourceGroup,
      region: provisionRegion,
      cluster_name: clusterName,
      node_sku: nodeSku,
      node_count: nodeCount,
      system_vm_size: systemVmSize,
      system_node_count: systemNodeCount,
      acr_resource_group: acrResourceGroup || "",
      acr_name: acrName || "",
      storage_resource_group: storageResourceGroup || "",
      storage_account: storageAccount || "",
    };
    reportClientError({
      level: "info",
      source: "cluster.provision.intent",
      message:
        `Create cluster clicked cluster=${clusterName} rg=${provisionResourceGroup} ` +
        `region=${provisionRegion} sku=${nodeSku} nodes=${nodeCount} ` +
        `system_sku=${systemVmSize} system_nodes=${systemNodeCount} tier=${tier || "none"}`,
    });
    if (preflightStatus !== "done" || !preflightResult) {
      setPreflightStatus("checking");
      setProvError(null);
      try {
        const res = await aksApi.preflight(preflightPayload);
        setPreflightResult(res);
        setPreflightStatus("done");
        if (!res.ok) {
          // Stop here; the modal renders the failing rows. Don't enqueue
          // the Celery task — the user has to fix the inputs first.
          return;
        }
      } catch (e) {
        setPreflightStatus("done");
        // Preflight outage is non-fatal: fall through to the provision
        // call so the canonical ARM error still reaches the user.
        setPreflightResult({
          ok: true,
          checks: [
            {
              name: "skus",
              status: "warn",
              message:
                "Could not run pre-flight (network/auth). Submitting anyway; " +
                "Azure will validate at provision time.",
            },
          ],
          portal_url: null,
        });
        console.warn("aks preflight failed", e);
      }
    } else if (!preflightResult.ok) {
      // Defensive — the disabled button + form guard already prevent
      // reaching this branch, but keep the early return just in case.
      return;
    }

    setProvStatus("creating");
    setProvError(null);
    setProvStart(Date.now());
    setTaskId(null);
    setTaskPhase(null);
    setTaskProgress(null);
    // The modal does not close synchronously here — it closes from the
    // two-stage effect above the moment the enqueue POST returns a
    // `taskId`. Until then the modal shows its "Creating…" button so the
    // user gets immediate feedback while the POST is in flight (a few
    // hundred ms). Closing on `taskId` (rather than waiting for ARM to
    // publish `cluster_state`, ~70 s later) means the user is not left
    // staring at a modal that merely duplicates the in-card progress
    // banner. If the enqueue POST itself fails, the catch below flips to
    // "error" and the card's ProvisionErrorCard surfaces it with the form
    // inputs preserved (they live in this hook, not the modal).
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
        tier,
      });
      // Capture the Celery task id so the poller above can drive the
      // banner. The api route returns several aliases (task_id /
      // instance_id / id) — pick whichever is present.
      const tid =
        response?.task_id ?? response?.instance_id ?? response?.id ?? null;
      if (tid) setTaskId(tid);
    } catch (e) {
      setProvError(formatApiError(e, "aks"));
      setProvStatus("error");
      // The enqueue POST failed before a taskId was issued, so the modal
      // never auto-closed. It stays open here so the user sees the error
      // in the structured error card and can fix the form or click Cancel
      // without losing inputs. (Once a taskId exists the modal has already
      // closed and any later FAILURE surfaces via the card's error card.)
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

  // P3-2 retry helper: hydrate the form from the persisted
  // `LastFailedProvision` slot so an Edit & retry from the dashboard
  // banner doesn't drop the user on a blank form. We mark RG and
  // region as user-touched so the auto-sync effects do not immediately
  // overwrite the values we just restored.
  const applyLastFailedContext = (ctx: {
    clusterName: string;
    region: string;
    resourceGroup: string;
  }) => {
    if (ctx.clusterName) setClusterName(ctx.clusterName);
    if (ctx.region) {
      setRegionUserTouched(true);
      setProvisionRegionState(ctx.region);
    }
    if (ctx.resourceGroup) {
      setRgUserTouched(true);
      setProvisionResourceGroupState(ctx.resourceGroup);
    }
  };

  const clusterNameValid = CLUSTER_NAME_RE.test(clusterName);

  const provisionResourceGroupValid = resourceGroupNameValid(provisionResourceGroup);
  // Whether an RG with this exact name already exists in the subscription.
  // **Not** a blocker — Azure happily hosts multiple AKS clusters in a
  // single RG, and the backend `provision_aks` task is idempotent
  // (`rc.resource_groups.get` first, only `create_or_update` on miss).
  // The modal surfaces this purely as an info note so the user knows the
  // existing RG will be reused rather than recreated. Case-insensitive
  // because Azure RG names are case-insensitive.
  const provisionResourceGroupExists = (existingResourceGroupNames ?? []).some(
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
    tier,
    setTier,
    provisionRegion,
    setProvisionRegion,
    resetProvisionRegionToDashboard,
    provisionResourceGroup,
    setProvisionResourceGroup,
    resetProvisionResourceGroupTracking,
    applyLastFailedContext,
    provisionResourceGroupValid,
    provisionResourceGroupExists,
    // status
    provStatus,
    setProvStatus,
    provError,
    setProvError,
    elapsed,
    /** Phase reported by the Celery task; null while we wait for the first
     *  poll to come back. Banner shows this so a stuck task looks stuck. */
    taskPhase,
    /** Full progress payload (step / total_steps / message / pools / …)
     *  for the banner to render rich sub-progress. */
    taskProgress,
    /** Pre-flight UI state — drives the modal's check list and the
     *  Create button label/disabled state. */
    preflightStatus,
    preflightResult,
    clusterNameValid,
    handleProvision,
    /** Clear a previously-reported provisioning error and return the
     *  modal to "edit the form" state. Wired to the error card's
     *  Dismiss / Edit-&-retry buttons. Safe to call from "done" /
     *  "idle" too — it just resets to "idle" + clears the error.
     *
     *  Also invalidates the cached preflight result: an ARM rejection
     *  proves the world has changed since our last preflight (or the
     *  preflight missed something), so the next Create click must
     *  re-run preflight from scratch instead of replaying the stale
     *  "ok" answer. */
    resetError: () => {
      setProvError(null);
      setProvStatus("idle");
      setPreflightStatus("idle");
      setPreflightResult(null);
    },
    /** Cancel an in-flight provision task. Sends `terminate=True` so
     *  the worker SIGTERMs out of the ARM poll loop. Worker may take
     *  up to one ARM poll interval (~20 s) to honor the signal — the
     *  banner keeps the spinner until the poller reports REVOKED, at
     *  which point the usual error path renders. No-op when there is
     *  no taskId or when the task is not currently running. */
    cancelProvision: async (): Promise<void> => {
      if (!taskId || provStatus !== "creating") return;
      try {
        await aksApi.cancelProvision(taskId);
        // Optimistic state — the poller will land the canonical REVOKED
        // shortly after. Until then the banner shows "Cancelling…".
        setProvError("Cancellation requested. Waiting for the worker to stop…");
      } catch (e) {
        setProvError(formatApiError(e, "aks"));
      }
    },
  };
}
