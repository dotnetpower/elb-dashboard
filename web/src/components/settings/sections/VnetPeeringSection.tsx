import { Copy, Loader2 } from "lucide-react";

import type { ResourceConfig } from "@/components/SetupWizard";
import { Badge, Field, Group, Row, Section, StatusLine } from "@/components/settings/primitives";
import { INPUT_STYLE, SELECT_STYLE } from "@/components/settings/styles";

import { ExistingPeerings } from "./vnetPeering/PeeringList";
import { NsgRuleAction } from "./vnetPeering/NsgRuleAction";
import { useVnetPeering } from "./vnetPeering/useVnetPeering";

export function VnetPeeringSection({ config }: { config: ResourceConfig | null }) {
  const {
    clusterName,
    setClusterName,
    availableClusters,
    clustersLoading,
    clustersLoaded,
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
    running,
    error,
    result,
    canSubmit,
    peer,
    probe,
    peerings,
    probeTone,
    copied,
    retrying,
    retryNote,
    copyRemediationCommand,
    retryAfterGrant,
    nsgRunning,
    nsgError,
    nsgResult,
    applyNsgRule,
    cancelNsgPreview,
    existing,
    existingLoading,
    existingError,
    loadExisting,
    dismissedPeerings,
    deletingPeering,
    peeringActionError,
    hidePeering,
    deletePeering,
  } = useVnetPeering(config);

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
