import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  Copy,
  EyeOff,
  Loader2,
  Network,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";

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
  type VnetPeeringExistingItem,
  type VnetPeeringExistingResponse,
  type VnetPeeringNsgRuleResponse,
  type VnetPeeringResponse,
} from "@/api/settings";
import type { ResourceConfig } from "@/components/SetupWizard";
import { Badge, Field, Group, Row, Section, StatusLine } from "@/components/settings/primitives";
import { INPUT_STYLE, SELECT_STYLE } from "@/components/settings/styles";
import { pickPreferredCluster } from "@/utils/clusterSelection";

import {
  dismissPeering,
  readDismissedPeerings,
} from "./dismissedPeerings";
import { classifyPeering } from "./peeringHealth";

export function VnetPeeringSection({ config }: { config: ResourceConfig | null }) {
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

  return (
    <Section heading="VNet peering (OpenAPI access)">
      <Group>
        <StatusLine kind="info">
          Peer a remote VNet with this cluster&apos;s AKS auto-VNet so VMs in
          that VNet can reach the elb-openapi private IP (auto-detected from the
          selected cluster&apos;s internal LoadBalancer). Bidirectional peering
          is created idempotently; the dashboard&apos;s managed identity needs
          <code> Network Contributor</code> on both sides.
        </StatusLine>
        <ExistingPeerings
          loading={existingLoading}
          error={existingError}
          data={existing}
          clusterName={clusterName}
          dismissed={dismissedPeerings}
          deletingPeering={deletingPeering}
          actionError={peeringActionError}
          onRefresh={() => void loadExisting()}
          onHide={hidePeering}
          onDelete={(name) => void deletePeering(name)}
        />
        <Field
          label="AKS cluster"
          hint={
            clustersLoading
              ? "Discovering AKS clusters in this subscription..."
              : availableClusters.length === 0 && clustersLoaded
                ? "No ELB-managed AKS clusters were found in this subscription."
                : "The cluster whose auto-VNet hosts elb-openapi."
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
              placeholder={
                clustersLoaded && availableClusters.length === 0
                  ? "No AKS cluster detected"
                  : "aks-..."
              }
              style={INPUT_STYLE}
            />
          )}
        </Field>
        <Field
          label="Target subscription"
          hint={subsLoading ? "Loading..." : "Subscription that owns the remote VNet."}
        >
          <select
            value={targetSubscriptionId}
            onChange={(event) => setTargetSubscriptionId(event.target.value)}
            style={SELECT_STYLE}
          >
            <option value="">Select subscription…</option>
            {subscriptions.map((s) => (
              <option key={s.subscriptionId} value={s.subscriptionId}>
                {s.displayName} ({s.subscriptionId.slice(0, 8)}…)
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="Target resource group"
          hint={rgLoading ? "Loading resource groups..." : "Resource group that holds the target VNet."}
        >
          <select
            value={targetResourceGroup}
            onChange={(event) => setTargetResourceGroup(event.target.value)}
            disabled={!targetSubscriptionId || rgLoading}
            style={SELECT_STYLE}
          >
            <option value="">Select resource group…</option>
            {resourceGroups.map((rg) => (
              <option key={rg.name} value={rg.name}>
                {rg.name} ({rg.location})
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="Target VNet"
          hint={
            vnetsLoading
              ? "Loading virtual networks..."
              : vnets.length === 0 && targetResourceGroup
                ? "No virtual networks in this resource group."
                : "The VNet whose VMs need to reach the OpenAPI private IP."
          }
        >
          <select
            value={targetVnetName}
            onChange={(event) => setTargetVnetName(event.target.value)}
            disabled={!targetResourceGroup || vnetsLoading}
            style={SELECT_STYLE}
          >
            <option value="">Select VNet…</option>
            {vnets.map((v) => (
              <option key={v.name} value={v.name}>
                {v.name} [{v.addressPrefixes.join(", ") || "?"}]
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="OpenAPI private IP"
          hint={
            targetIpResolving
              ? "Detecting the elb-openapi internal-LB IP for this cluster…"
              : targetIpAutoNote ??
                "Internal-LB IP exposed by the elb-openapi Service. Auto-detected from the selected cluster; override if needed."
          }
        >
          <input
            value={targetIp}
            onChange={(event) => {
              targetIpTouchedRef.current = true;
              setTargetIpAutoNote(null);
              setTargetIp(event.target.value);
            }}
            placeholder={targetIpResolving ? "Detecting…" : "Auto-detected from cluster"}
            style={INPUT_STYLE}
          />
        </Field>
        <Field label="Probe path" hint="Path appended to the IP for the post-peering reachability check.">
          <input
            value={targetPath}
            onChange={(event) => setTargetPath(event.target.value)}
            placeholder="/openapi.json"
            style={INPUT_STYLE}
          />
        </Field>
        <div
          style={{
            display: "flex",
            gap: 8,
            alignItems: "center",
            flexWrap: "wrap",
            paddingBottom: 14,
          }}
        >
          <button
            className="glass-button glass-button--primary"
            onClick={peer}
            disabled={!canSubmit || running || retrying}
            style={{ fontSize: 12 }}
          >
            {running ? "Peering..." : "Peer & probe"}
          </button>
          {running && (
            <span
              style={{
                fontSize: 11,
                color: "var(--text-muted)",
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
              }}
            >
              <Loader2 size={12} className="spin" /> creating peerings + probing
            </span>
          )}
        </div>
        {result && (
          <>
            <Row
              label="Probe"
              control={
                <Badge tone={probeTone}>
                  {probe
                    ? probe.reachable
                      ? `${probe.status_code ?? 200} OK · ${probe.latency_ms}ms`
                      : `Unreachable${probe.status_code ? ` (${probe.status_code})` : ""}`
                    : "n/a"}
                </Badge>
              }
              hint={probe?.url}
            />
            {peerings.length > 0 && (
              <StatusLine kind="info">
                {peerings
                  .map((p) => `${p.direction}: ${p.name} → ${p.state}`)
                  .join(" · ")}
              </StatusLine>
            )}
            {result.skipped && result.reason && (
              <StatusLine kind="info">
                Skipped: {result.message ?? result.reason}
              </StatusLine>
            )}
            {result.error && (
              <StatusLine kind="error">{result.error}</StatusLine>
            )}
            {result.rbac_remediation && (
              <>
                <StatusLine kind="error">
                  {result.rbac_remediation.message}{" "}
                  <code>{result.rbac_remediation.command}</code>
                </StatusLine>
                <div
                  style={{
                    display: "flex",
                    gap: 8,
                    alignItems: "center",
                    flexWrap: "wrap",
                    paddingBottom: 4,
                  }}
                >
                  <button
                    className="glass-button"
                    onClick={copyRemediationCommand}
                    style={{ fontSize: 12, display: "inline-flex", alignItems: "center", gap: 6 }}
                  >
                    <Copy size={12} /> {copied ? "Copied" : "Copy command"}
                  </button>
                  <button
                    className="glass-button glass-button--primary"
                    onClick={retryAfterGrant}
                    disabled={retrying || !canSubmit}
                    style={{ fontSize: 12, display: "inline-flex", alignItems: "center", gap: 6 }}
                  >
                    {retrying && <Loader2 size={12} className="spin" />}
                    {retrying ? "Retrying…" : "I granted the role — retry"}
                  </button>
                </div>
                {retryNote && <StatusLine kind="info">{retryNote}</StatusLine>}
              </>
            )}
            {probe && !probe.reachable && probe.message && (
              <StatusLine kind="error">Probe error: {probe.message}</StatusLine>
            )}
            {probe && !probe.reachable && (
              <NsgRuleAction
                running={nsgRunning}
                disabled={!canSubmit}
                onPreview={() => applyNsgRule(true)}
                onConfirm={() => applyNsgRule(false)}
                onCancel={cancelNsgPreview}
                result={nsgResult}
                error={nsgError}
              />
            )}
            {result.recovery_command && (
              <StatusLine kind="info">
                Recovery (paste in terminal):{" "}
                <code>{result.recovery_command}</code>
              </StatusLine>
            )}
          </>
        )}
        {error && <StatusLine kind="error">{error}</StatusLine>}
      </Group>
    </Section>
  );
}

function peeringStateTone(state: string): "success" | "warning" | "muted" {
  const normalised = state.toLowerCase();
  if (normalised === "connected") return "success";
  if (normalised === "initiated") return "warning";
  return "muted";
}

function PeeringFlag({ on, label }: { on: boolean; label: string }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        fontSize: 10,
        textTransform: "uppercase",
        letterSpacing: "0.04em",
        color: on ? "var(--text-muted)" : "var(--text-faint)",
        opacity: on ? 1 : 0.55,
      }}
      title={`${label}: ${on ? "allowed" : "blocked"}`}
    >
      {on ? <Check size={10} /> : <X size={10} />}
      {label}
    </span>
  );
}

function ExistingPeeringRow({
  item,
  deleting,
  onHide,
  onDelete,
}: {
  item: VnetPeeringExistingItem;
  deleting: boolean;
  onHide: () => void;
  onDelete: () => void;
}) {
  const remote = item.remote_vnet;
  const remoteLabel = remote?.name || item.name || "(unknown VNet)";
  const subShort = remote?.subscription_id ? `${remote.subscription_id.slice(0, 8)}…` : "";
  const locationBits = [remote?.resource_group, subShort].filter(Boolean).join(" · ");
  const prefixes = item.remote_address_prefixes.join(", ");
  const health = classifyPeering(item);
  const stale = health !== "healthy";

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 6,
        padding: "10px 12px",
        borderRadius: 8,
        background: "var(--bg-tertiary)",
        border: stale
          ? "1px solid var(--warning-border, var(--border-weak))"
          : "1px solid var(--border-weak)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 10,
        }}
      >
        <div style={{ minWidth: 0 }}>
          <div
            style={{
              fontSize: 13,
              color: "var(--text-primary)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
            title={remote?.id || item.name}
          >
            {remoteLabel}
          </div>
          {locationBits && (
            <div style={{ fontSize: 11, color: "var(--text-faint)" }}>{locationBits}</div>
          )}
        </div>
        <Badge tone={peeringStateTone(item.peering_state)}>{item.peering_state}</Badge>
      </div>
      {prefixes && (
        <div
          style={{
            fontSize: 11,
            color: "var(--text-muted)",
            fontFamily: "var(--font-mono, monospace)",
            wordBreak: "break-word",
          }}
        >
          {prefixes}
        </div>
      )}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
        <PeeringFlag on={item.allow_virtual_network_access} label="vnet access" />
        <PeeringFlag on={item.allow_forwarded_traffic} label="forwarded" />
        <PeeringFlag on={item.allow_gateway_transit} label="gw transit" />
        <PeeringFlag on={item.use_remote_gateways} label="remote gw" />
      </div>
      {stale && (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 8,
            marginTop: 4,
            padding: "8px 10px",
            borderRadius: 6,
            background: "var(--warning-surface, rgba(180, 140, 60, 0.08))",
            border: "1px solid var(--warning-border, var(--border-weak))",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "flex-start",
              gap: 6,
              fontSize: 11.5,
              color: "var(--text-muted)",
              lineHeight: 1.4,
            }}
          >
            <AlertTriangle
              size={13}
              strokeWidth={1.5}
              style={{ flexShrink: 0, marginTop: 1, color: "var(--warning, #c79a3a)" }}
            />
            <span>
              {health === "ghost"
                ? "The remote VNet for this peering no longer exists. This is a stale peering — delete it to clean up, or hide it from this view."
                : "This peering is disconnected (its remote VNet may have been deleted). If it is no longer needed, delete it to clean up, or hide it from this view."}
            </span>
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            <button
              type="button"
              className="glass-button"
              onClick={onDelete}
              disabled={deleting}
              title="Delete this stale peering from the AKS VNet"
              style={{
                fontSize: 11,
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "4px 10px",
              }}
            >
              {deleting ? (
                <Loader2 size={11} className="spin" />
              ) : (
                <Trash2 size={11} strokeWidth={1.5} />
              )}
              {deleting ? "Deleting…" : "Delete peering"}
            </button>
            <button
              type="button"
              className="glass-button"
              onClick={onHide}
              disabled={deleting}
              title="Hide this peering from the dashboard (does not touch Azure)"
              style={{
                fontSize: 11,
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "4px 10px",
              }}
            >
              <EyeOff size={11} strokeWidth={1.5} />
              Hide
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function ExistingPeerings({
  loading,
  error,
  data,
  clusterName,
  dismissed,
  deletingPeering,
  actionError,
  onRefresh,
  onHide,
  onDelete,
}: {
  loading: boolean;
  error: string | null;
  data: VnetPeeringExistingResponse | null;
  clusterName: string;
  dismissed: Set<string>;
  deletingPeering: string | null;
  actionError: string | null;
  onRefresh: () => void;
  onHide: (peeringName: string) => void;
  onDelete: (peeringName: string) => void;
}) {
  const allPeerings = data?.peerings ?? [];
  const peerings = allPeerings.filter((p) => !dismissed.has(p.name));
  const hiddenCount = allPeerings.length - peerings.length;
  const showRefresh = Boolean(clusterName);

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 8,
        padding: 12,
        marginTop: 4,
        marginBottom: 12,
        borderRadius: 8,
        background: "var(--surface-2, var(--bg-secondary))",
        border: "1px solid var(--border-subtle, var(--border-weak))",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 10,
        }}
      >
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            fontSize: 12,
            fontWeight: 600,
            color: "var(--text-muted)",
          }}
        >
          <Network size={13} strokeWidth={1.5} /> Existing peerings
          {data?.aks_vnet_name ? (
            <span style={{ fontWeight: 400, color: "var(--text-faint)" }}>
              on {data.aks_vnet_name}
            </span>
          ) : null}
        </span>
        {showRefresh && (
          <button
            type="button"
            className="glass-button"
            onClick={onRefresh}
            disabled={loading}
            aria-label="Refresh existing peerings"
            title="Refresh"
            style={{
              fontSize: 11,
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "4px 8px",
            }}
          >
            <RefreshCw size={11} className={loading ? "spin" : undefined} />
            Refresh
          </button>
        )}
      </div>

      {!clusterName ? (
        <StatusLine kind="info">
          Select an AKS cluster to see the peerings already on its VNet.
        </StatusLine>
      ) : loading && !data ? (
        <StatusLine kind="loading">Loading existing peerings…</StatusLine>
      ) : error ? (
        <StatusLine kind="error">{error}</StatusLine>
      ) : data?.error ? (
        <StatusLine kind="error">
          Could not list peerings: {data.error}. The dashboard managed identity
          may lack <code>Network Contributor</code> read access on this
          cluster&apos;s VNet.
        </StatusLine>
      ) : data?.skipped ? (
        <StatusLine kind="info">
          No AKS auto-VNet to inspect
          {data.reason === "aks_node_rg_has_no_vnet"
            ? " — this cluster runs in a BYO subnet (no peering needed; VMs in that VNet reach the OpenAPI IP directly)."
            : data.reason
              ? ` (${data.reason}).`
              : "."}
        </StatusLine>
      ) : peerings.length === 0 ? (
        <StatusLine kind="info">
          {hiddenCount > 0
            ? `All ${hiddenCount} peering(s) on this cluster's AKS VNet are hidden. Use the form below to create one.`
            : "No peerings on this cluster's AKS VNet yet. Use the form below to create one."}
        </StatusLine>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {peerings.map((item) => (
            <ExistingPeeringRow
              key={item.name}
              item={item}
              deleting={deletingPeering === item.name}
              onHide={() => onHide(item.name)}
              onDelete={() => onDelete(item.name)}
            />
          ))}
        </div>
      )}
      {actionError && <StatusLine kind="error">{actionError}</StatusLine>}
      {hiddenCount > 0 && peerings.length > 0 && (
        <StatusLine kind="info">
          {hiddenCount} stale peering(s) hidden from this view.
        </StatusLine>
      )}
    </div>
  );
}

function NsgRuleAction({
  running,
  disabled,
  onPreview,
  onConfirm,
  onCancel,
  result,
  error,
}: {
  running: boolean;
  disabled: boolean;
  onPreview: () => void;
  onConfirm: () => void;
  onCancel: () => void;
  result: VnetPeeringNsgRuleResponse | null;
  error: string | null;
}) {
  const [copied, setCopied] = useState<"idle" | "ok" | "failed">("idle");
  // Carry the priority the *preview* step quoted so we can flag a shift
  // if another operator took our slot before the operator clicked
  // Confirm. We capture in a ref to keep the effect dependency tight
  // (effect only re-runs when ``result`` changes).
  const previewedPriorityRef = useRef<number | null>(null);
  const [priorityShift, setPriorityShift] = useState<{ from: number; to: number } | null>(null);

  useEffect(() => {
    if (!result) {
      previewedPriorityRef.current = null;
      setPriorityShift(null);
      return;
    }
    if (result.dry_run === true) {
      previewedPriorityRef.current = result.rule?.priority ?? null;
      setPriorityShift(null);
      return;
    }
    if (result.applied === true) {
      const previewed = previewedPriorityRef.current;
      const actual = result.rule?.priority;
      if (
        previewed !== null &&
        typeof actual === "number" &&
        actual !== previewed
      ) {
        setPriorityShift({ from: previewed, to: actual });
      } else {
        setPriorityShift(null);
      }
    }
  }, [result]);

  const copyCli = async () => {
    if (!result?.cli_hint) return;
    if (typeof navigator === "undefined" || !navigator.clipboard) {
      setCopied("failed");
      window.setTimeout(() => setCopied("idle"), 2500);
      return;
    }
    try {
      await navigator.clipboard.writeText(result.cli_hint);
      setCopied("ok");
      window.setTimeout(() => setCopied("idle"), 1500);
    } catch {
      // Clipboard API can refuse without user gesture or under permissions
      // policies; surface a hint so the operator knows to select + Ctrl+C
      // by hand instead of silently doing nothing.
      setCopied("failed");
      window.setTimeout(() => setCopied("idle"), 2500);
    }
  };

  const skipReason = result?.skipped_reason;
  const isAllowedBySkip = !!result && result.applied && skipReason === "already_present";
  const isDryRunPreview = !!result && !result.applied && skipReason === "dry_run";
  const previewRule = result?.rule;
  const previewName = result?.planned_rule_name ?? previewRule?.rule_name ?? "(deterministic)";

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 8,
        padding: 12,
        borderRadius: 8,
        background: "var(--surface-2)",
        border: "1px solid var(--border-subtle)",
      }}
    >
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
        {!isDryRunPreview && (
          <button
            type="button"
            className="btn btn-secondary"
            onClick={onPreview}
            disabled={running || disabled}
            style={{ minWidth: 180 }}
          >
            {running ? (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <Loader2 size={12} className="spin" /> Checking NSG...
              </span>
            ) : (
              "Preview NSG rule (80, 443)"
            )}
          </button>
        )}
        {isDryRunPreview && (
          <>
            <button
              type="button"
              className="btn btn-primary"
              onClick={onConfirm}
              disabled={running || disabled}
              style={{ minWidth: 180 }}
            >
              {running ? (
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <Loader2 size={12} className="spin" /> Applying NSG rule...
                </span>
              ) : (
                "Confirm & apply"
              )}
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={onCancel}
              disabled={running}
            >
              Cancel
            </button>
          </>
        )}
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
          {isDryRunPreview
            ? "Review the planned rule below, then confirm to write it to ARM."
            : "Previews an inbound-allow rule on the target subnet's NSG (source = AKS VNet CIDR, destination = target_ip/32, ports = 80,443). No ARM write happens until you confirm."}{" "}
          Requires{" "}
          <code>Microsoft.Network/networkSecurityGroups/securityRules/write</code>.
        </span>
      </div>
      {isDryRunPreview && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "max-content 1fr",
            columnGap: 12,
            rowGap: 4,
            padding: "8px 12px",
            borderRadius: 6,
            background: "var(--surface-3)",
            border: "1px solid var(--border-subtle)",
            fontSize: 12,
          }}
        >
          <span style={{ color: "var(--text-muted)" }}>Planned name</span>
          <code>{previewName}</code>
          <span style={{ color: "var(--text-muted)" }}>Planned priority</span>
          <code>{previewRule?.priority ?? "(first free in 4000-4096)"}</code>
          <span style={{ color: "var(--text-muted)" }}>Source CIDRs</span>
          <code>{(previewRule?.source_prefixes ?? []).join(", ") || "(none)"}</code>
          <span style={{ color: "var(--text-muted)" }}>Destination</span>
          <code>{previewRule?.destination_ip}/32</code>
          <span style={{ color: "var(--text-muted)" }}>Ports</span>
          <code>{(previewRule?.ports ?? []).join(", ")}</code>
          <span style={{ color: "var(--text-muted)" }}>NSG</span>
          <code style={{ wordBreak: "break-all" }}>
            {result?.nsg_context?.nsg_name ?? previewRule?.nsg_id}
          </code>
        </div>
      )}
      {result?.applied && (
        <StatusLine kind="success">
          {isAllowedBySkip
            ? `Existing rule already covers this probe (${result.rule?.rule_name}, priority ${result.rule?.priority ?? "?"}).`
            : `Rule applied: ${result.rule?.rule_name} (priority ${result.rule?.priority}, ports ${(result.rule?.ports ?? []).join(", ")}). Re-running probe...`}
        </StatusLine>
      )}
      {priorityShift && (
        <StatusLine kind="info">
          Priority changed between preview and confirm:{" "}
          <code>{priorityShift.from}</code> -&gt;{" "}
          <code>{priorityShift.to}</code>. Another operator took your
          slot; the rule still applied at the next free priority in
          4000-4096.
        </StatusLine>
      )}
      {result && !result.applied && skipReason === "no_nsg_attached" && (
        <StatusLine kind="info">
          The target subnet has no NSG attached, so nothing to update. If the
          probe still fails, check the AKS subnet&apos;s NSG, Azure Firewall,
          or User Defined Routes.
        </StatusLine>
      )}
      {result && !result.applied && skipReason === "target_ip_not_in_any_subnet" && (
        <StatusLine kind="error">
          {result.target_ip} is not inside any subnet of the selected target VNet.
        </StatusLine>
      )}
      {result && !result.applied && skipReason === "permission_denied" && (
        <>
          <StatusLine kind="info">
            Your identity does not have NSG write permission. Run this with a
            privileged identity instead:
          </StatusLine>
          {result.cli_hint && (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 6,
                fontFamily: "var(--font-mono)",
                fontSize: 11,
              }}
            >
              <code style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
                {result.cli_hint}
              </code>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={copyCli}
                  style={{ alignSelf: "flex-start" }}
                >
                  <Copy size={12} />{" "}
                  {copied === "ok"
                    ? "Copied!"
                    : copied === "failed"
                      ? "Copy failed"
                      : "Copy CLI"}
                </button>
                {copied === "failed" && (
                  <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                    Clipboard blocked — select the snippet and press Ctrl+C
                    (or &#8984;C) instead.
                  </span>
                )}
              </div>
            </div>
          )}
        </>
      )}
      {result && !result.applied && skipReason === "name_collision" && (
        <>
          <StatusLine kind="error">
            A rule with the same name already exists but its scope differs.
            The dashboard will not overwrite operator rules — review the
            existing rule below in Azure Portal before retrying.
          </StatusLine>
          {result.rule?.conflict_existing && (
            <ConflictExistingPanel
              conflict={result.rule.conflict_existing as Record<string, unknown>}
            />
          )}
        </>
      )}
      {result && !result.applied && skipReason === "no_free_priority" && (
        <StatusLine kind="error">
          No free priority in the 4000-4096 reserved range. Free one in
          Azure Portal and retry.
        </StatusLine>
      )}
      {error && <StatusLine kind="error">{error}</StatusLine>}
    </div>
  );
}

/**
 * Renders the existing NSG rule that collides with the deterministic
 * dashboard rule name. Shape mirrors
 * `api.tasks.azure.peering_nsg._summarise_rule` — every field is
 * optional because the SDK can return either snake_case or
 * SDK-attribute spellings via the helper.
 */
function ConflictExistingPanel({
  conflict,
}: {
  conflict: Record<string, unknown>;
}) {
  const asString = (key: string): string => {
    const v = conflict[key];
    return v === null || v === undefined ? "" : String(v);
  };
  const asList = (key: string): string[] => {
    const v = conflict[key];
    return Array.isArray(v) ? v.map((item) => String(item)) : [];
  };

  const name = asString("name");
  const priority = asString("priority");
  const protocol = asString("protocol");
  const access = asString("access");
  const direction = asString("direction");
  const sourcePrefixes = asList("source_address_prefixes").filter(Boolean);
  const sourcePorts = asList("source_port_ranges").filter(Boolean);
  const destPrefix = asString("destination_address_prefix");
  // Azure NSG can return either `destination_address_prefix` (singular
  // string) or `destination_address_prefixes` (list). The backend
  // summariser surfaces both shapes; merge them so the panel renders
  // the full destination set whichever form the existing rule used.
  // `"*"` is Azure's explicit wildcard sentinel — render it as "Any"
  // so operators don't read it as a literal CIDR.
  const destPrefixesList = asList("destination_address_prefixes").filter(Boolean);
  const renderWildcard = (raw: string): string => (raw === "*" ? "Any" : raw);
  const destDisplay =
    destPrefixesList.length > 0
      ? destPrefixesList.map(renderWildcard).join(", ")
      : destPrefix
        ? renderWildcard(destPrefix)
        : "(any)";
  const destPorts = asList("destination_port_ranges").filter(Boolean);
  const description = asString("description");

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "max-content 1fr",
        columnGap: 12,
        rowGap: 4,
        padding: "8px 12px",
        borderRadius: 6,
        background: "var(--surface-3)",
        border: "1px solid var(--border-subtle)",
        fontSize: 12,
      }}
    >
      <span style={{ color: "var(--text-muted)" }}>Existing name</span>
      <code style={{ wordBreak: "break-all" }}>{name || "(unknown)"}</code>
      <span style={{ color: "var(--text-muted)" }}>Priority</span>
      <code>{priority || "(unknown)"}</code>
      <span style={{ color: "var(--text-muted)" }}>Direction</span>
      <code>{direction || "(unknown)"}</code>
      <span style={{ color: "var(--text-muted)" }}>Access</span>
      <code>{access || "(unknown)"}</code>
      <span style={{ color: "var(--text-muted)" }}>Protocol</span>
      <code>{protocol || "(unknown)"}</code>
      <span style={{ color: "var(--text-muted)" }}>Source CIDRs</span>
      <code style={{ wordBreak: "break-all" }}>
        {sourcePrefixes.length
          ? sourcePrefixes.map(renderWildcard).join(", ")
          : "(any)"}
      </code>
      {sourcePorts.length > 0 && (
        <>
          <span style={{ color: "var(--text-muted)" }}>Source ports</span>
          <code>{sourcePorts.map(renderWildcard).join(", ")}</code>
        </>
      )}
      <span style={{ color: "var(--text-muted)" }}>Destination</span>
      <code style={{ wordBreak: "break-all" }}>{destDisplay}</code>
      <span style={{ color: "var(--text-muted)" }}>Destination ports</span>
      <code>{destPorts.length ? destPorts.map(renderWildcard).join(", ") : "(any)"}</code>
      {description && (
        <>
          <span style={{ color: "var(--text-muted)" }}>Description</span>
          <code style={{ wordBreak: "break-all" }}>{description}</code>
        </>
      )}
    </div>
  );
}
