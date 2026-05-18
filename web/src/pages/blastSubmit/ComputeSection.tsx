import { Loader2, Server, Zap } from "lucide-react";
import { Link } from "react-router-dom";

import type { AksClusterSummary, WarmupDbInfo } from "@/api/endpoints";
import type { BlastWarmupPlan } from "@/api/blast";
import type { FormState } from "@/pages/blastSubmitModel";
import type { SetBlastField } from "@/pages/blastSubmit/types";
import {
  getWorkloadNodeCount,
  getWorkloadNodeSku,
  selectWorkloadPool,
} from "@/pages/blastSubmit/computeEnvironment";
import type { ShardingAvailability } from "@/pages/blastSubmit/shardingAvailability";
import { SectionHeader } from "@/pages/blastSubmit/ui";

export function ComputeSection({
  subId,
  workloadRg,
  clusters,
  selectedCluster,
  clusterLoading,
  form,
  set,
  isDbAlreadyWarm,
  warmDbInfo,
  selectedDbShortName,
  dbShardSets,
  warmupPlan,
  shardingAvailability,
}: {
  subId: string;
  workloadRg: string;
  clusters: AksClusterSummary[];
  selectedCluster?: AksClusterSummary;
  clusterLoading: boolean;
  form: FormState;
  set: SetBlastField;
  isDbAlreadyWarm: boolean;
  warmDbInfo?: WarmupDbInfo;
  selectedDbShortName: string;
  /** Sorted preset N values that are pre-built (e.g. [1,2,3,4,5,6,8,10]). */
  dbShardSets?: number[];
  /**
   * Server-computed warmup feasibility for the selected DB on the selected
   * cluster. Drives the inline advisory below the warmup checkbox and the
   * submit-blocking logic in the parent page.
   */
  warmupPlan?: BlastWarmupPlan;
  shardingAvailability: ShardingAvailability;
}) {
  const shardingOptions = [
    shardingAvailability.options.off,
    shardingAvailability.options.approximate,
    shardingAvailability.options.precise,
  ];
  const selectedShardingOption = shardingAvailability.options[form.sharding_mode];
  const hasShardedMode =
    shardingAvailability.options.approximate.enabled ||
    shardingAvailability.options.precise.enabled;
  const shardedUnavailableReason =
    shardingAvailability.options.precise.reason ?? shardingAvailability.options.approximate.reason;
  const offUnavailableReason = shardingAvailability.options.off.reason;

  return (
    <section className="glass-card blast-section">
      <SectionHeader
        step={5}
        icon={<Server size={16} strokeWidth={1.5} />}
        title="Compute Environment"
        subtitle="Select an AKS cluster to run the search"
      />
      {!subId && <div className="muted">Configure your Azure resources on the Dashboard first.</div>}
      {subId && clusterLoading && (
        <div className="muted">
          <Loader2 size={12} className="spin" style={{ display: "inline", verticalAlign: "middle" }} /> Loading clusters...
        </div>
      )}
      {subId && clusters.length === 0 && !clusterLoading && (
        <div className="muted">
          No AKS clusters in <strong>{workloadRg}</strong>.{" "}
          <Link to="/" style={{ color: "var(--accent)" }}>
            Create one on the Dashboard
          </Link>
          .
        </div>
      )}
      {clusters.length > 0 && (
        <>
          <select
            className="glass-input"
            value={form.selectedCluster}
            onChange={(event) => set("selectedCluster", event.target.value)}
            style={{ marginBottom: 12 }}
          >
            <option value="">— Select cluster —</option>
            {clusters.map((cluster) => (
              <option key={cluster.name} value={cluster.name}>
                {cluster.name} — {cluster.region} ({cluster.power_state ?? "?"})
              </option>
            ))}
          </select>
          {selectedCluster && <ClusterInfo cluster={selectedCluster} />}
        </>
      )}

      {selectedCluster && (
        <div
          style={{
            marginTop: 12,
            padding: "10px 14px",
            background: "var(--glass-bg)",
            border: "1px solid var(--glass-border)",
            borderRadius: 8,
          }}
        >
          <div
            style={{
              fontSize: 12,
              fontWeight: 600,
              marginBottom: 8,
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <Zap size={14} style={{ color: "var(--warning)" }} />
            Performance
          </div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 10,
              padding: "7px 9px",
              borderRadius: 6,
              background: isDbAlreadyWarm ? "rgba(106,214,163,0.08)" : "rgba(255,255,255,0.04)",
              border: `1px solid ${isDbAlreadyWarm ? "rgba(106,214,163,0.24)" : "var(--glass-border)"}`,
              fontSize: 11,
              marginBottom: 10,
            }}
          >
            <span>
              DB cache{" "}
              {isDbAlreadyWarm ? (
                <span style={{ color: "var(--success)", fontWeight: 500 }}>
                  Ready on {warmDbInfo?.nodes_ready}/{warmDbInfo?.total_jobs} nodes
                </span>
              ) : (
                <span className="muted">Not warmed on this cluster</span>
              )}
            </span>
            <span className="muted" style={{ textAlign: "right" }}>
              {isDbAlreadyWarm
                ? hasShardedMode
                  ? "Prepared shards available when capacity fits"
                  : "Full DB cache ready; sharded modes unavailable"
                : "Run baseline mode or warm the DB first"}
            </span>
          </div>
          <div style={{ marginTop: 8, marginBottom: 6 }}>
            <div className="muted" style={{ fontSize: 10, marginBottom: 6 }}>
              DB sharding mode
            </div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {shardingOptions.map((option) => {
                const active = form.sharding_mode === option.mode;
                return (
                  <button
                    key={option.mode}
                    type="button"
                    className="glass-button"
                    disabled={!option.enabled}
                    title={option.reason ?? option.description}
                    onClick={() => {
                      if (!option.enabled) return;
                      set("sharding_mode", option.mode);
                      set("db_auto_partition", option.mode !== "off");
                      set("disable_sharding", false);
                    }}
                    style={{
                      minHeight: 28,
                      padding: "4px 10px",
                      borderRadius: 6,
                      fontSize: 11,
                      cursor: option.enabled ? "pointer" : "not-allowed",
                      background: active ? "rgba(122,167,255,0.18)" : "var(--glass-bg)",
                      borderColor: active ? "rgba(122,167,255,0.45)" : "var(--glass-border)",
                      color: active ? "var(--text-primary)" : option.enabled ? "var(--text-muted)" : "var(--text-faint)",
                      opacity: option.enabled ? 1 : 0.48,
                    }}
                  >
                    {option.label}
                  </button>
                );
              })}
            </div>
            <div className="muted" style={{ fontSize: 10, marginTop: 6, lineHeight: 1.5 }}>
              {selectedShardingOption.description}
            </div>
            {!selectedShardingOption.enabled && selectedShardingOption.reason && (
              <div style={{ color: "var(--warning)", fontSize: 10, marginTop: 4, lineHeight: 1.5 }}>
                {selectedShardingOption.reason}
              </div>
            )}
            {form.sharding_mode === "off" && shardedUnavailableReason && (
              <div style={{ color: "var(--warning)", fontSize: 10, marginTop: 4, lineHeight: 1.5 }}>
                Sharded modes disabled: {shardedUnavailableReason}
              </div>
            )}
            {offUnavailableReason && form.sharding_mode !== "off" && (
              <div style={{ color: "var(--text-faint)", fontSize: 10, marginTop: 4, lineHeight: 1.5 }}>
                Off disabled: {offUnavailableReason}
              </div>
            )}
          </div>
          {isDbAlreadyWarm && (
            <div className="muted" style={{ fontSize: 10, marginTop: 6, lineHeight: 1.5 }}>
              {selectedDbShortName} is already loaded on this cluster. BLAST can reuse node-local DB files instead of downloading before each run.
            </div>
          )}
          <WarmupPlanAdvisory
            plan={warmupPlan}
            warmupRequested={form.enable_warmup && !isDbAlreadyWarm}
            onDisableWarmup={() => set("enable_warmup", false)}
          />
          <ShardingPreview
            dbShardSets={dbShardSets}
            capacityPlan={shardingAvailability.capacityPlan}
          />
        </div>
      )}
    </section>
  );
}

interface ShardingPreviewProps {
  dbShardSets?: number[];
  capacityPlan: ShardingAvailability["capacityPlan"];
}

/**
 * Auto-shard preview chip + opt-out toggle.
 *
 * Visible only when both the selected DB has been warmed/sharded and the
 * cluster context is known. Mirrors the backend selection in
 * `api/services/blast_config.py::generate_config` (auto_shard_eligible
 * branch) so the user sees the same N before they submit.
 *
 * The opt-out is intentionally low-prominence ("Disable sharding" with a
 * strike-through preview when active) — sharding is the preferred path
 * after the v3 benchmark and we want callers to think before turning it
 * off.
 */
function ShardingPreview({
  dbShardSets,
  capacityPlan,
}: ShardingPreviewProps) {
  if (!capacityPlan || !dbShardSets || dbShardSets.length === 0) {
    // No sharding metadata yet (or DB not warmed) — render nothing rather
    // than a misleading placeholder.
    return null;
  }
  const accent = capacityPlan.feasible ? "var(--accent)" : "var(--warning)";

  return (
    <div
      style={{
        marginTop: 10,
        paddingTop: 10,
        borderTop: "1px dashed var(--glass-border)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          flexWrap: "wrap",
          fontSize: 11,
          color: "var(--text-muted)",
        }}
      >
        <span
          style={{
            fontSize: 10,
            padding: "1px 6px",
            borderRadius: 3,
            color: accent,
            background: capacityPlan.feasible ? "rgba(110,159,255,0.10)" : "rgba(240,198,116,0.08)",
            border: `1px solid ${capacityPlan.feasible ? "rgba(110,159,255,0.28)" : "rgba(240,198,116,0.28)"}`,
            fontWeight: 500,
            whiteSpace: "nowrap",
            letterSpacing: 0.1,
          }}
          title={`Pre-built shard layouts: N = ${dbShardSets.join(", ")}. Selected against ${capacityPlan.numNodes}-node ${capacityPlan.machineType} cluster within safe RAM headroom.`}
        >
          Shard capacity · N={capacityPlan.pickedN}
        </span>
        <span>
          {capacityPlan.numNodes} {capacityPlan.numNodes === 1 ? "node" : "nodes"} · {capacityPlan.machineType.replace("Standard_", "")}
          {" · "}
          ~{capacityPlan.perShardGib < 10 ? capacityPlan.perShardGib.toFixed(1) : Math.round(capacityPlan.perShardGib)} GiB/shard
        </span>
      </div>
      {!capacityPlan.feasible && capacityPlan.reason && (
        <div style={{ color: "var(--warning)", fontSize: 10, marginTop: 5, lineHeight: 1.45 }}>
          {capacityPlan.reason}
        </div>
      )}
    </div>
  );
}

function ClusterInfo({ cluster }: { cluster: AksClusterSummary }) {
  const workloadPool = selectWorkloadPool(cluster);
  const workloadNodeSku = getWorkloadNodeSku(cluster);
  const workloadNodeCount = getWorkloadNodeCount(cluster);
  const rows: [string, string | null | undefined, string | undefined][] = [
    ["Status", cluster.power_state, cluster.power_state === "Running" ? "var(--success)" : "var(--warning)"],
    ["State", cluster.provisioning_state, undefined],
    ["NodePool", workloadPool ? `${workloadPool.name} (${workloadPool.mode ?? "User"})` : undefined, undefined],
    ["SKU", workloadNodeSku, undefined],
    ["Nodes", workloadNodeCount == null ? undefined : String(workloadNodeCount), undefined],
    ["K8s", cluster.k8s_version, undefined],
    ["Region", cluster.region, undefined],
  ];

  return (
    <div className="blast-cluster-info">
      {rows.map(([label, value, color]) => (
        <div key={label} className="blast-cluster-info__cell">
          <div className="blast-cluster-info__label">{label}</div>
          <div className="blast-cluster-info__value" style={color ? { fontWeight: 600, color } : undefined}>
            {value ?? "?"}
          </div>
        </div>
      ))}
    </div>
  );
}

interface WarmupPlanAdvisoryProps {
  plan?: BlastWarmupPlan;
  /**
   * True when the form requests warmup AND the DB isn't already cached.
   * Drives the colour and the "blocked" copy — when warmup isn't being
   * requested, an infeasible plan is just an FYI ("you'll be slow but
   * it'll work").
   */
  warmupRequested: boolean;
  onDisableWarmup: () => void;
}

/**
 * Inline advisory under the warmup checkbox that mirrors the planner
 * verdict from `/api/blast/databases?...&num_nodes=...&machine_type=...`.
 *
 * Three visual states:
 * - hidden — no plan, or plan is `ok`.
 * - amber ("Warmup advisory") — plan is non-ok but `feasible=true`
 *   (e.g. `ok_unknown_sku`), or plan is infeasible but warmup isn't
 *   being requested.
 * - red ("Warmup blocked") — plan is infeasible AND warmup is requested.
 *   In this case we also offer a one-click "Disable warmup" button so
 *   the user can submit anyway (they'll just run without the cache).
 *
 * No business logic — purely renders state passed by the parent. The
 * parent (BlastSubmit) is the source of truth for whether submit is
 * allowed.
 */
function WarmupPlanAdvisory({ plan, warmupRequested, onDisableWarmup }: WarmupPlanAdvisoryProps) {
  if (!plan || plan.status === "ok") return null;
  // Degenerate planner outputs (e.g. `no_db_size`, `no_nodes`) carry no
  // actionable signal for the user — the chips on the dashboard already
  // surface those. Hide them here to keep the submit page focused.
  if (plan.status === "no_db_size" || plan.status === "no_nodes") return null;

  const blocked = plan.feasible === false && warmupRequested;
  const role = blocked ? "alert" : "note";
  const accent = blocked ? "var(--danger)" : "var(--warning)";
  const heading = blocked ? "Warmup blocked" : "Warmup advisory";

  return (
    <div
      role={role}
      style={{
        marginTop: 8,
        padding: "8px 10px",
        borderRadius: 6,
        background: blocked ? "rgba(224,123,138,0.08)" : "rgba(240,198,116,0.08)",
        border: `1px solid ${blocked ? "rgba(224,123,138,0.32)" : "rgba(240,198,116,0.28)"}`,
        fontSize: 11,
        lineHeight: 1.45,
      }}
    >
      <div style={{ fontWeight: 600, color: accent, marginBottom: 2 }}>{heading}</div>
      <div className="muted">{plan.message}</div>
      {plan.recommendations.length > 0 && (
        <ul style={{ margin: "4px 0 0 16px", padding: 0 }}>
          {plan.recommendations.map((r) => (
            <li key={r} style={{ color: "var(--text-muted)" }}>
              {r}
            </li>
          ))}
        </ul>
      )}
      {blocked && (
        <button
          type="button"
          className="glass-button"
          onClick={onDisableWarmup}
          style={{
            marginTop: 6,
            fontSize: 10,
            padding: "3px 8px",
            minHeight: 22,
            borderRadius: 4,
          }}
        >
          Disable warmup and submit anyway
        </button>
      )}
    </div>
  );
}
