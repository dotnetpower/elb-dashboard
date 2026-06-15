/**
 * State + side-effect orchestration for {@link VnetPeeringSection}.
 *
 * Owns every piece of the VNet-peering panel that is not presentation: the
 * ARM cascade (subscription → resource group → VNet dropdowns), AKS cluster
 * discovery, live auto-detection of the elb-openapi internal-LB IP, the
 * read-only "already peered" list, orphaned-peering housekeeping, the
 * peer+probe round-trip, the RBAC-remediation copy/retry loop, and the NSG
 * rule preview/apply flow. The section component consumes the returned model
 * and renders only.
 *
 * Every async path folds Azure faults into surfaced banners (never throws to
 * the component); monotonic tokens guard against stale responses landing last
 * after a rapid cluster switch.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  listResourceGroups,
  listSubscriptions,
  listVnets,
  type ResourceGroupSummary,
  type SubscriptionSummary,
  type VirtualNetworkSummary,
} from "@/api/arm";
import { formatApiError } from "@/api/client";
import { type AksClusterSummary, monitoringApi } from "@/api/monitoring";
import {
  settingsApi,
  type VnetPeeringExistingResponse,
  type VnetPeeringNsgRuleResponse,
  type VnetPeeringResponse,
} from "@/api/settings";
import type { ResourceConfig } from "@/components/SetupWizard";
import { pickPreferredCluster } from "@/utils/clusterSelection";

import { dismissPeering, readDismissedPeerings } from "../dismissedPeerings";

export function useVnetPeering(config: ResourceConfig | null) {
  const [clusterName, setClusterName] = useState("");
  const [availableClusters, setAvailableClusters] = useState<AksClusterSummary[]>([]);
  const [clustersLoading, setClustersLoading] = useState(false);
  const [clustersLoaded, setClustersLoaded] = useState(false);

  const [targetSubscriptionId, setTargetSubscriptionId] = useState("");
  const [subscriptions, setSubscriptions] = useState<SubscriptionSummary[]>([]);
  const [subsLoading, setSubsLoading] = useState(false);
  const [targetResourceGroup, setTargetResourceGroup] = useState("");
  const [resourceGroups, setResourceGroups] = useState<ResourceGroupSummary[]>([]);
  const [rgLoading, setRgLoading] = useState(false);
  const [targetVnetName, setTargetVnetName] = useState("");
  const [vnets, setVnets] = useState<VirtualNetworkSummary[]>([]);
  const [vnetsLoading, setVnetsLoading] = useState(false);
  const [targetIp, setTargetIp] = useState("");
  // Auto-detect of the elb-openapi internal-LB IP for the selected cluster.
  // `targetIpTouchedRef` flips the moment the operator edits the field by
  // hand so a later cluster switch / re-resolve never clobbers their value.
  const [targetIpResolving, setTargetIpResolving] = useState(false);
  const [targetIpAutoNote, setTargetIpAutoNote] = useState<string | null>(null);
  const targetIpTouchedRef = useRef(false);
  const [targetPath, setTargetPath] = useState("/openapi.json");

  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<VnetPeeringResponse | null>(null);

  // RBAC-remediation affordances: copy the grant command, and re-probe in a
  // loop after the operator runs it (Azure role propagation takes 1-5 min, so
  // the first manual retry usually still fails — this absorbs that delay).
  const [copied, setCopied] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [retryNote, setRetryNote] = useState<string | null>(null);

  const [nsgRunning, setNsgRunning] = useState(false);
  const [nsgError, setNsgError] = useState<string | null>(null);
  const [nsgResult, setNsgResult] = useState<VnetPeeringNsgRuleResponse | null>(null);

  // Read-only view of the peerings already present on the selected cluster's
  // AKS VNet. Auto-loaded on cluster change and re-loaded after any write so
  // the operator sees the live state without leaving the panel.
  const [existing, setExisting] = useState<VnetPeeringExistingResponse | null>(null);
  const [existingLoading, setExistingLoading] = useState(false);
  const [existingError, setExistingError] = useState<string | null>(null);

  // Orphaned-peering housekeeping: which ghost peerings the operator has hidden
  // (cosmetic, localStorage), which one is mid-delete, and the last delete error.
  const [dismissedPeerings, setDismissedPeerings] = useState<Set<string>>(() => new Set());
  const [deletingPeering, setDeletingPeering] = useState<string | null>(null);
  const [peeringActionError, setPeeringActionError] = useState<string | null>(null);

  const subscriptionId = config?.subscriptionId ?? "";
  const selectedClusterRg =
    availableClusters.find((c) => c.name === clusterName)?.resource_group ??
    config?.workloadResourceGroup ??
    "";

  // Subscription dropdown — mirror the ARM hop the wizard uses so an
  // operator can pick a *different* subscription as the peering target
  // (the dashboard's MI must have Network Contributor on both sides).
  useEffect(() => {
    let cancelled = false;
    setSubsLoading(true);
    void (async () => {
      try {
        const subs = await listSubscriptions();
        if (cancelled) return;
        setSubscriptions(subs);
        setTargetSubscriptionId((current) => {
          if (current && subs.some((s) => s.subscriptionId === current)) return current;
          return subscriptionId || subs[0]?.subscriptionId || "";
        });
      } catch (err) {
        if (cancelled) return;
        setError(formatApiError(err, "arm"));
      } finally {
        if (!cancelled) setSubsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [subscriptionId]);

  // Resource-group dropdown for the target subscription.
  useEffect(() => {
    if (!targetSubscriptionId) {
      setResourceGroups([]);
      return;
    }
    let cancelled = false;
    setRgLoading(true);
    void (async () => {
      try {
        const rgs = await listResourceGroups(targetSubscriptionId);
        if (cancelled) return;
        setResourceGroups(rgs);
        setTargetResourceGroup((current) =>
          current && rgs.some((r) => r.name === current) ? current : "",
        );
      } catch (err) {
        if (cancelled) return;
        setError(formatApiError(err, "arm"));
        setResourceGroups([]);
      } finally {
        if (!cancelled) setRgLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [targetSubscriptionId]);

  // VNets in the selected RG.
  useEffect(() => {
    if (!targetSubscriptionId || !targetResourceGroup) {
      setVnets([]);
      return;
    }
    let cancelled = false;
    setVnetsLoading(true);
    void (async () => {
      try {
        const items = await listVnets(targetSubscriptionId, targetResourceGroup);
        if (cancelled) return;
        setVnets(items);
        setTargetVnetName((current) =>
          current && items.some((v) => v.name === current) ? current : items[0]?.name ?? "",
        );
      } catch (err) {
        if (cancelled) return;
        setError(formatApiError(err, "arm"));
        setVnets([]);
      } finally {
        if (!cancelled) setVnetsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [targetSubscriptionId, targetResourceGroup]);

  // AKS cluster discovery — same shape as PublicHttpsSection so an
  // ElasticBLAST workload cluster outside the anchor RG still shows up.
  useEffect(() => {
    if (!subscriptionId) return;
    let cancelled = false;
    setClustersLoading(true);
    void (async () => {
      try {
        const response = await monitoringApi.aks(subscriptionId);
        if (cancelled) return;
        const clusters = (response.clusters ?? []).filter((c) => c.name);
        setAvailableClusters(clusters);
        setClustersLoaded(true);
        setClusterName((current) => {
          if (current && clusters.some((c) => c.name === current)) return current;
          const preferred = pickPreferredCluster(clusters, {
            resourceGroup: config?.workloadResourceGroup,
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
  }, [subscriptionId, config?.workloadResourceGroup]);

  // Auto-detect the elb-openapi internal-LB IP for the selected cluster.
  // The internal-LB IP is per-cluster (a BYO-subnet cluster lands inside the
  // platform VNet, an auto-VNet cluster lands in 10.224.0.0/16), so there is
  // no safe single default — resolve it live whenever the cluster selection
  // changes, unless the operator has typed an override.
  useEffect(() => {
    if (targetIpTouchedRef.current) return;
    if (!subscriptionId || !selectedClusterRg || !clusterName) {
      setTargetIpAutoNote(null);
      return;
    }
    let cancelled = false;
    setTargetIpResolving(true);
    setTargetIpAutoNote(null);
    void (async () => {
      try {
        const svc = await monitoringApi.serviceIp(
          subscriptionId,
          selectedClusterRg,
          clusterName,
          "elb-openapi",
        );
        if (cancelled || targetIpTouchedRef.current) return;
        if (svc.external_ip) {
          setTargetIp(svc.external_ip);
          setTargetIpAutoNote(`Auto-detected from ${clusterName}.`);
        } else {
          setTargetIpAutoNote(
            "elb-openapi has no internal-LB IP yet (Service pending or not deployed). " +
              "Enter the IP manually once it is assigned.",
          );
        }
      } catch {
        if (cancelled || targetIpTouchedRef.current) return;
        setTargetIpAutoNote(
          "Could not auto-detect the elb-openapi IP; enter it manually.",
        );
      } finally {
        if (!cancelled) setTargetIpResolving(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [subscriptionId, selectedClusterRg, clusterName]);

  // Load the read-only "already peered" list for the selected cluster. The
  // helper never raises on a routine Azure fault — it folds RBAC denials and
  // BYO self-VNet skips into the 200 payload — so a thrown error here is a
  // hard transport/5xx failure worth surfacing as a banner. A monotonic token
  // guards against a rapid cluster switch landing a stale response last.
  const existingSeqRef = useRef(0);
  const loadExisting = useCallback(async () => {
    if (!subscriptionId || !selectedClusterRg || !clusterName) {
      existingSeqRef.current += 1;
      setExisting(null);
      setExistingError(null);
      setExistingLoading(false);
      return;
    }
    const token = (existingSeqRef.current += 1);
    setExistingLoading(true);
    setExistingError(null);
    try {
      const response = await settingsApi.listExistingPeerings(
        subscriptionId,
        selectedClusterRg,
        clusterName,
      );
      if (token !== existingSeqRef.current) return;
      setExisting(response);
    } catch (err) {
      if (token !== existingSeqRef.current) return;
      setExisting(null);
      setExistingError(formatApiError(err, "settings"));
    } finally {
      if (token === existingSeqRef.current) setExistingLoading(false);
    }
  }, [subscriptionId, selectedClusterRg, clusterName]);

  useEffect(() => {
    void loadExisting();
  }, [loadExisting]);

  // Re-read the per-cluster hidden-ghost set whenever the selected cluster
  // changes so a hidden row stays hidden across cluster switches / reloads.
  useEffect(() => {
    setDismissedPeerings(readDismissedPeerings(clusterName));
    setPeeringActionError(null);
  }, [clusterName]);

  // Hide a ghost peering from the list (cosmetic only — never touches Azure).
  const hidePeering = useCallback(
    (peeringName: string) => {
      setDismissedPeerings(dismissPeering(clusterName, peeringName));
    },
    [clusterName],
  );

  // Delete an orphaned peering from the AKS VNet, then refresh the list.
  const deletePeering = useCallback(
    async (peeringName: string) => {
      if (!subscriptionId || !selectedClusterRg || !clusterName) return;
      setDeletingPeering(peeringName);
      setPeeringActionError(null);
      try {
        await settingsApi.deletePeering({
          subscription_id: subscriptionId,
          resource_group: selectedClusterRg,
          cluster_name: clusterName,
          peering_name: peeringName,
        });
        await loadExisting();
      } catch (err) {
        setPeeringActionError(formatApiError(err, "settings"));
      } finally {
        setDeletingPeering(null);
      }
    },
    [subscriptionId, selectedClusterRg, clusterName, loadExisting],
  );

  const canSubmit = Boolean(
    subscriptionId &&
      selectedClusterRg &&
      clusterName &&
      targetSubscriptionId &&
      targetResourceGroup &&
      targetVnetName &&
      targetIp,
  );

  const peer = async () => {
    if (!canSubmit) return;
    setError(null);
    setResult(null);
    setNsgError(null);
    setNsgResult(null);
    setRetryNote(null);
    setRunning(true);
    try {
      const response = await callPeer();
      setResult(response);
      void loadExisting();
    } catch (err) {
      setError(formatApiError(err, "settings"));
    } finally {
      setRunning(false);
    }
  };

  // Single peering round-trip, shared by the manual button, the post-grant
  // retry loop, and the post-NSG re-probe so they all send identical args.
  const callPeer = () =>
    settingsApi.peerVnet({
      subscription_id: subscriptionId,
      resource_group: selectedClusterRg,
      cluster_name: clusterName,
      target_subscription_id: targetSubscriptionId,
      target_resource_group: targetResourceGroup,
      target_vnet_name: targetVnetName,
      target_ip: targetIp || undefined,
      target_path: targetPath || undefined,
    });

  const copyRemediationCommand = async () => {
    const command = result?.rbac_remediation?.command;
    if (!command) return;
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2200);
    } catch {
      // Clipboard API can be blocked (insecure context / permissions) — the
      // command stays visible in the <code> block for manual selection.
      setCopied(false);
    }
  };

  // Poll peerVnet after the operator grants the role. Azure RBAC propagation
  // is 1-5 min, so we retry on a backoff and stop as soon as the response no
  // longer carries an RBAC denial (rbac_remediation absent) — that means both
  // peering directions finally succeeded.
  const retryAfterGrant = async () => {
    if (!canSubmit || retrying) return;
    const delaysMs = [10000, 20000, 30000, 30000, 30000, 30000];
    setRetrying(true);
    setError(null);
    try {
      for (let attempt = 0; attempt < delaysMs.length; attempt += 1) {
        setRetryNote(
          `Waiting for role propagation, then retrying (${attempt + 1}/${delaysMs.length})…`,
        );
        await new Promise((resolve) => window.setTimeout(resolve, delaysMs[attempt]));
        let response: VnetPeeringResponse;
        try {
          response = await callPeer();
        } catch (err) {
          setError(formatApiError(err, "settings"));
          setRetryNote(null);
          return;
        }
        setResult(response);
        if (!response.rbac_remediation) {
          void loadExisting();
          setRetryNote(
            response.error
              ? "Peering no longer blocked by RBAC; see the result above."
              : "Peering succeeded — RBAC grant has propagated.",
          );
          return;
        }
      }
      setRetryNote(
        "Still blocked after retrying. Confirm the role assignment landed on the " +
          "target VNet, then retry again.",
      );
    } finally {
      setRetrying(false);
    }
  };

  const applyNsgRule = async (dryRun: boolean = true) => {
    if (!canSubmit) return;
    setNsgError(null);
    if (dryRun) {
      // Preview is a fresh round-trip — wipe any previous applied/skipped
      // banner so the operator can't confuse a stale result with the new
      // plan.
      setNsgResult(null);
    }
    setNsgRunning(true);
    try {
      const response = await settingsApi.applyPeeringNsgRule({
        subscription_id: subscriptionId,
        resource_group: selectedClusterRg,
        cluster_name: clusterName,
        target_subscription_id: targetSubscriptionId,
        target_resource_group: targetResourceGroup,
        target_vnet_name: targetVnetName,
        target_ip: targetIp || undefined,
        dry_run: dryRun,
      });
      setNsgResult(response);
      if (!dryRun && response.applied) {
        void loadExisting();
        // Re-run the probe so the operator sees the unblocked state in one go.
        try {
          const reProbe = await callPeer();
          setResult(reProbe);
        } catch {
          // Probe failure here is informational only — the NSG rule
          // was applied; just leave the previous result on screen.
        }
      }
    } catch (err) {
      setNsgError(formatApiError(err, "settings"));
    } finally {
      setNsgRunning(false);
    }
  };

  const cancelNsgPreview = () => {
    setNsgResult(null);
    setNsgError(null);
  };

  const probe = result?.probe;
  const peerings = result?.peerings ?? [];
  const probeTone: "success" | "muted" | "warning" = probe
    ? probe.reachable
      ? "success"
      : "warning"
    : "muted";

  return {
    // cluster selection
    clusterName,
    setClusterName,
    availableClusters,
    clustersLoading,
    clustersLoaded,
    // target ARM cascade
    targetSubscriptionId,
    setTargetSubscriptionId,
    subscriptions,
    subsLoading,
    targetResourceGroup,
    setTargetResourceGroup,
    resourceGroups,
    rgLoading,
    targetVnetName,
    setTargetVnetName,
    vnets,
    vnetsLoading,
    targetIp,
    setTargetIp,
    targetIpResolving,
    targetIpAutoNote,
    setTargetIpAutoNote,
    targetIpTouchedRef,
    targetPath,
    setTargetPath,
    // peer + probe
    running,
    error,
    result,
    canSubmit,
    peer,
    probe,
    peerings,
    probeTone,
    // RBAC remediation
    copied,
    retrying,
    retryNote,
    copyRemediationCommand,
    retryAfterGrant,
    // NSG rule
    nsgRunning,
    nsgError,
    nsgResult,
    applyNsgRule,
    cancelNsgPreview,
    // existing peerings + housekeeping
    existing,
    existingLoading,
    existingError,
    loadExisting,
    dismissedPeerings,
    deletingPeering,
    peeringActionError,
    hidePeering,
    deletePeering,
  };
}
