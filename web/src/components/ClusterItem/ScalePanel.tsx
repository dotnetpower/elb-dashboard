import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Server } from "lucide-react";

import { aksApi } from "@/api/aks";
import type { AutoWarmupPreference } from "@/api/monitoring";
import { DB_CATALOG } from "@/components/cards/storageDbCatalog";
import { readAutoWarmupDbs } from "@/components/cards/storage/autoWarmupPrefs";
import { PermissionGate } from "@/components/PermissionGate";
import { useToast } from "@/components/Toast";
import { usePermissions } from "@/hooks/usePermissions";
import { formatApiError } from "@/api/client";

import { clampNodeCount, sliderMaxFor } from "./scaleNodeCount";

// Glassmorphic workload-pool scaling control surfaced inside the expanded
// cluster card. The operator picks a target node count with a slider + number
// input (the two are always in sync) and presses Apply; the backend resizes the
// blastpool and — when Auto warm databases are configured — chains a forced
// warmup reconcile so re-scaled nodes get their node-local BLAST DB cache.
//
// Scope: the workload (blastpool) pool only. The system pool is never touched.

export interface ScalePanelProps {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  /** Current workload (blastpool) node count. */
  currentNodeCount: number;
  clusterIsRunning: boolean;
  /** Auto warm context — when a storage account + warm databases exist the
   *  scale call carries an `auto_warmup` preference so the backend re-warms. */
  machineType?: string;
  storageAccount?: string;
  storageResourceGroup?: string;
  region?: string;
  acrResourceGroup?: string;
  acrName?: string;
  terminalResourceGroup?: string;
  terminalVmName?: string;
}

export function ScalePanel({
  subscriptionId,
  resourceGroup,
  clusterName,
  currentNodeCount,
  clusterIsRunning,
  machineType,
  storageAccount,
  storageResourceGroup,
  region,
  acrResourceGroup,
  acrName,
  terminalResourceGroup,
  terminalVmName,
}: ScalePanelProps) {
  const qc = useQueryClient();
  const { toast } = useToast();
  const { permissions } = usePermissions(subscriptionId, resourceGroup, clusterName);

  const current = Math.max(1, currentNodeCount || 1);
  const sliderMax = sliderMaxFor(current);
  const [target, setTarget] = useState<number>(current);
  // When the live count changes (e.g. a scale completed, or the user expanded a
  // different cluster row) re-anchor the draft to the new current value so the
  // control reflects reality instead of a stale draft.
  useEffect(() => {
    setTarget(current);
  }, [current]);

  const warmDbCount = useMemo(() => readAutoWarmupDbs().size, []);

  const scaleMutation = useMutation({
    mutationFn: (nodeCount: number) => {
      const databases = [...readAutoWarmupDbs()].sort();
      const programs = Object.fromEntries(
        databases.map((dbName) => {
          const catalog = DB_CATALOG.find((item) => item.value === dbName);
          return [dbName, catalog?.type === "prot" ? "blastp" : "blastn"];
        }),
      );
      const autoWarmup: Partial<AutoWarmupPreference> | undefined =
        storageAccount && databases.length > 0
          ? {
              subscription_id: subscriptionId,
              resource_group: resourceGroup,
              cluster_name: clusterName,
              storage_account: storageAccount,
              storage_resource_group: storageResourceGroup || resourceGroup,
              region,
              databases,
              programs,
              enabled: true,
              acr_resource_group: acrResourceGroup,
              acr_name: acrName,
              terminal_resource_group: terminalResourceGroup,
              terminal_vm_name: terminalVmName,
              machine_type: machineType || undefined,
              num_nodes: nodeCount,
            }
          : undefined;
      return aksApi.scale(subscriptionId, resourceGroup, clusterName, nodeCount, autoWarmup);
    },
    onSuccess: (_data, nodeCount) => {
      qc.invalidateQueries({ queryKey: ["aks"] });
      const warmNote =
        warmDbCount > 0
          ? " Warm-up will re-run for the new node set."
          : "";
      toast(
        `Scaling ${clusterName} to ${nodeCount} node${nodeCount === 1 ? "" : "s"}.${warmNote}`,
        "success",
      );
    },
    onError: (error: unknown) => {
      toast(`Could not scale cluster: ${formatApiError(error, "aks")}`, "error");
    },
  });

  const clampedTarget = clampNodeCount(target, sliderMax);
  const unchanged = clampedTarget === current;
  const disabled = !clusterIsRunning || scaleMutation.isPending;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 8,
        padding: "10px 12px",
        background: "var(--glass-surface, rgba(255,255,255,0.03))",
        border: "1px solid var(--glass-border, rgba(255,255,255,0.08))",
        borderRadius: 8,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Server size={14} strokeWidth={1.5} />
        <span style={{ fontSize: 13, fontWeight: 600 }}>Workload nodes</span>
        <span className="muted" style={{ fontSize: 12, marginLeft: "auto" }}>
          Currently {current} node{current === 1 ? "" : "s"}
        </span>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <input
          type="range"
          min={1}
          max={sliderMax}
          step={1}
          value={clampedTarget}
          disabled={disabled}
          onChange={(e) => setTarget(clampNodeCount(Number(e.target.value), sliderMax))}
          aria-label="Workload node count"
          style={{ flex: 1, accentColor: "var(--accent, #6ea8fe)" }}
        />
        <input
          type="number"
          min={1}
          max={sliderMax}
          step={1}
          value={clampedTarget}
          disabled={disabled}
          onChange={(e) => setTarget(clampNodeCount(Number(e.target.value), sliderMax))}
          aria-label="Workload node count (exact)"
          className="glass-input"
          style={{ width: 64, textAlign: "center", fontSize: 13 }}
        />
      </div>

      <div
        className="muted"
        style={{ fontSize: 11, display: "flex", justifyContent: "space-between" }}
      >
        <span>
          {warmDbCount > 0
            ? "Warm-up re-runs automatically after scaling."
            : "No Auto warm databases configured — scaling only."}
        </span>
        <PermissionGate need="can_write" permissions={permissions}>
          <button
            type="button"
            className="glass-button glass-button--primary"
            disabled={disabled || unchanged}
            onClick={() => scaleMutation.mutate(clampedTarget)}
            style={{ fontSize: 12, padding: "4px 12px" }}
            title={
              !clusterIsRunning
                ? "Start the cluster before scaling"
                : unchanged
                  ? "Pick a different node count to apply"
                  : `Scale to ${clampedTarget} node${clampedTarget === 1 ? "" : "s"}`
            }
          >
            {scaleMutation.isPending ? (
              <>
                <Loader2 size={12} className="spin" style={{ marginRight: 4 }} />
                Scaling…
              </>
            ) : (
              "Apply"
            )}
          </button>
        </PermissionGate>
      </div>
    </div>
  );
}
