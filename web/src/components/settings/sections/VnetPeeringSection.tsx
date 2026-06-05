import { useEffect, useRef, useState } from "react";
import { Copy, Loader2 } from "lucide-react";

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
  type VnetPeeringNsgRuleResponse,
  type VnetPeeringResponse,
} from "@/api/settings";
import type { ResourceConfig } from "@/components/SetupWizard";
import { Badge, Field, Group, Row, Section, StatusLine } from "@/components/settings/primitives";
import { INPUT_STYLE } from "@/components/settings/styles";
import { pickPreferredCluster } from "@/utils/clusterSelection";

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
              style={INPUT_STYLE}
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
            style={INPUT_STYLE}
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
            style={INPUT_STYLE}
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
            style={INPUT_STYLE}
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
