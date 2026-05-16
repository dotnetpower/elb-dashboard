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
import { SectionHeader } from "@/pages/blastSubmit/ui";
import { selectPartitionsForSubmit } from "@/utils/dbSharding";

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
  dbSharded,
  dbShardSets,
  dbTotalBytes,
  warmupPlan,
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
  /** Selected DB has pre-built shard layouts in storage. */
  dbSharded?: boolean;
  /** Sorted preset N values that are pre-built (e.g. [1,2,3,4,5,6,8,10]). */
  dbShardSets?: number[];
  /** DB size in bytes — used to compute the auto-pick N for the preview. */
  dbTotalBytes?: number;
  /**
   * Server-computed warmup feasibility for the selected DB on the selected
   * cluster. Drives the inline advisory below the warmup checkbox and the
   * submit-blocking logic in the parent page.
   */
  warmupPlan?: BlastWarmupPlan;
}) {
  return (
    <section className="glass-card blast-section">
      <SectionHeader
        step={4}
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
          <label
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              cursor: isDbAlreadyWarm ? "default" : "pointer",
              fontSize: 12,
              marginBottom: 6,
              opacity: isDbAlreadyWarm ? 0.8 : 1,
            }}
          >
            <input
              type="checkbox"
              checked={isDbAlreadyWarm || form.enable_warmup}
              disabled={isDbAlreadyWarm}
              onChange={(event) => set("enable_warmup", event.target.checked)}
              style={{ accentColor: isDbAlreadyWarm ? "var(--success)" : "var(--accent)" }}
            />
            <span>
              Warmup cluster{" "}
              {isDbAlreadyWarm ? (
                <span style={{ color: "var(--success)", fontWeight: 500 }}>
                  — cached on {warmDbInfo?.nodes_ready}/{warmDbInfo?.total_jobs} nodes
                </span>
              ) : (
                <span className="muted">(prepare DB shards on local SSD before BLAST)</span>
              )}
            </span>
          </label>
          <div style={{ marginTop: 8, marginBottom: 6 }}>
            <div className="muted" style={{ fontSize: 10, marginBottom: 6 }}>
              DB sharding mode
            </div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {[
                ["off", "Off"],
                ["approximate", "Fast shard"],
                ["precise", "Precise shard"],
              ].map(([mode, label]) => {
                const active = form.sharding_mode === mode;
                return (
                  <button
                    key={mode}
                    type="button"
                    className="glass-button"
                    onClick={() => {
                      set("sharding_mode", mode as FormState["sharding_mode"]);
                      set("db_auto_partition", mode !== "off");
                    }}
                    style={{
                      minHeight: 28,
                      padding: "4px 10px",
                      borderRadius: 6,
                      fontSize: 11,
                      background: active ? "rgba(122,167,255,0.18)" : "var(--glass-bg)",
                      borderColor: active ? "rgba(122,167,255,0.45)" : "var(--glass-border)",
                      color: active ? "var(--text-primary)" : "var(--text-muted)",
                    }}
                  >
                    {label}
                  </button>
                );
              })}
            </div>
            {form.sharding_mode !== "off" && (
              <div className="muted" style={{ fontSize: 10, marginTop: 6, lineHeight: 1.5 }}>
                {form.sharding_mode === "precise"
                  ? "Precise shard requires single-query metadata, tabular output, and effective search space."
                  : "Fast shard uses prepared DB shards and may differ from full-DB BLAST."}
              </div>
            )}
          </div>
          {(form.enable_warmup || isDbAlreadyWarm) && (
            <div className="muted" style={{ fontSize: 10, marginTop: 6, lineHeight: 1.5 }}>
              {isDbAlreadyWarm
                ? `${selectedDbShortName} is already loaded on all cluster nodes. BLAST will start immediately without download delay.`
                : "The prepare step will create the cluster, download DB shards to node SSDs, then submit BLAST with reuse=true. This adds ~5-10 min setup but significantly improves search performance for large databases."}
            </div>
          )}
          <WarmupPlanAdvisory
            plan={warmupPlan}
            warmupRequested={form.enable_warmup && !isDbAlreadyWarm}
            onDisableWarmup={() => set("enable_warmup", false)}
          />
          <ShardingPreview
            cluster={selectedCluster}
            dbSharded={dbSharded}
            dbShardSets={dbShardSets}
            dbTotalBytes={dbTotalBytes}
            disabled={form.disable_sharding}
            onToggleDisabled={(value) => set("disable_sharding", value)}
          />
        </div>
      )}
    </section>
  );
}

interface ShardingPreviewProps {
  cluster: AksClusterSummary;
  dbSharded?: boolean;
  dbShardSets?: number[];
  dbTotalBytes?: number;
  disabled: boolean;
  onToggleDisabled: (value: boolean) => void;
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
  cluster,
  dbSharded,
  dbShardSets,
  dbTotalBytes,
  disabled,
  onToggleDisabled,
}: ShardingPreviewProps) {
  if (!dbSharded || !dbShardSets || dbShardSets.length === 0) {
    // No sharding metadata yet (or DB not warmed) — render nothing rather
    // than a misleading placeholder.
    return null;
  }
  const numNodes = getWorkloadNodeCount(cluster) || 1;
  const sku = getWorkloadNodeSku(cluster) || "Standard_E32s_v5";
  const totalBytes = dbTotalBytes && dbTotalBytes > 0 ? dbTotalBytes : 0;
  // Always route through the helper so `pickedN` is guaranteed to be one
  // of the pre-built presets (matches what the backend would pick). When
  // the DB size is unknown we still want N >= numNodes — the helper
  // handles that branch with `minByRam = 0`.
  const pickedN = selectPartitionsForSubmit(totalBytes, numNodes, sku, dbShardSets);
  const perShardGib = totalBytes > 0 ? totalBytes / 1024 ** 3 / pickedN : null;

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
          color: disabled ? "var(--text-faint)" : "var(--text-muted)",
        }}
      >
        <span
          style={{
            fontSize: 10,
            padding: "1px 6px",
            borderRadius: 3,
            color: disabled ? "var(--text-faint)" : "var(--accent)",
            background: disabled
              ? "rgba(255,255,255,0.04)"
              : "rgba(110,159,255,0.10)",
            border: `1px solid ${disabled ? "var(--glass-border)" : "rgba(110,159,255,0.28)"}`,
            fontWeight: 500,
            whiteSpace: "nowrap",
            letterSpacing: 0.1,
            textDecoration: disabled ? "line-through" : "none",
          }}
          title={`Pre-built shard layouts: N = ${dbShardSets.join(", ")}. Auto-selected to fit ${numNodes}-node ${sku} cluster within safe RAM headroom.`}
        >
          Auto-shard · N={pickedN}
        </span>
        <span style={{ textDecoration: disabled ? "line-through" : "none" }}>
          {numNodes} {numNodes === 1 ? "node" : "nodes"} · {sku.replace("Standard_", "")}
          {perShardGib !== null && (
            <> · ~{perShardGib < 10 ? perShardGib.toFixed(1) : Math.round(perShardGib)} GiB/shard</>
          )}
        </span>
      </div>
      <label
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          marginTop: 6,
          fontSize: 10,
          color: "var(--text-faint)",
          cursor: "pointer",
        }}
      >
        <input
          type="checkbox"
          checked={disabled}
          onChange={(event) => onToggleDisabled(event.target.checked)}
          style={{ accentColor: "var(--warning)", width: 11, height: 11 }}
        />
        <span>
          Disable sharding{" "}
          <span style={{ color: "var(--text-faint)" }}>
            (advanced — single-volume mode is significantly slower)
          </span>
        </span>
      </label>
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
