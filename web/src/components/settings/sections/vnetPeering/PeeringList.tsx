/**
 * VNet peering — existing-peerings list presentation.
 *
 * `ExistingPeerings` (the list container with refresh + degraded states),
 * `ExistingPeeringRow` (one peering with stale/ghost remediation actions),
 * `PeeringFlag`, and the `peeringStateTone` mapper. Pure presentation; the
 * parent `VnetPeeringSection` owns the data fetch and action handlers.
 */

import {
  AlertTriangle,
  Check,
  EyeOff,
  Loader2,
  Network,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";

import {
  type VnetPeeringExistingItem,
  type VnetPeeringExistingResponse,
} from "@/api/settings";
import { Badge, StatusLine } from "@/components/settings/primitives";
import { classifyPeering } from "../peeringHealth";

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

export function ExistingPeerings({
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
