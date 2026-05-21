import { useEffect } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, Loader2, Plus, X } from "lucide-react";

import type { AksSku } from "@/api/endpoints";
import {
  describeAksSku,
  formatAksSkuOption,
  groupAksSkus,
} from "@/hooks/useAksSkus";

import { MAX_SYSTEM_NODE_COUNT } from "./useClusterProvisioning";

export function ProvisionModal({
  // form state
  clusterName,
  setClusterName,
  clusterNameValid,
  nodeSku,
  setNodeSku,
  nodeCount,
  setNodeCount,
  systemVmSize,
  setSystemVmSize,
  systemNodeCount,
  setSystemNodeCount,
  // sku catalog
  skuOptions,
  groupLabels,
  groupOrder,
  // context
  region,
  resourceGroup,
  // status
  provStatus,
  provError,
  // actions
  onSubmit,
  onClose,
}: {
  clusterName: string;
  setClusterName: (v: string) => void;
  clusterNameValid: boolean;
  nodeSku: string;
  setNodeSku: (v: string) => void;
  nodeCount: number;
  setNodeCount: (v: number) => void;
  systemVmSize: string;
  setSystemVmSize: (v: string) => void;
  systemNodeCount: number;
  setSystemNodeCount: (v: number) => void;
  skuOptions: AksSku[];
  groupLabels: Record<string, string>;
  groupOrder: string[];
  region?: string;
  resourceGroup: string;
  provStatus: string;
  provError: string | null;
  onSubmit: () => void;
  onClose: () => void;
}) {
  // ESC to close.
  useEffect(() => {
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleEsc);
    return () => window.removeEventListener("keydown", handleEsc);
  }, [onClose]);

  const selectedSku = skuOptions.find((option) => option.name === nodeSku);
  const selectedSystemSku = skuOptions.find(
    (option) => option.name === systemVmSize,
  );
  const blastCost = (selectedSku?.hourlyUsd ?? 1.34) * nodeCount;
  const systemCost = (selectedSystemSku?.hourlyUsd ?? 0.096) * systemNodeCount;
  const estimatedCost = blastCost + systemCost;

  const blastGroups = groupAksSkus(skuOptions, "blast", groupOrder, groupLabels);
  const systemGroups = groupAksSkus(skuOptions, "system", groupOrder, groupLabels);

  return createPortal(
    <div
      className="provision-modal-backdrop"
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.6)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 200,
        backdropFilter: "blur(4px)",
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="provision-modal-card"
        style={{
          background: "var(--bg-primary)",
          border: "1px solid var(--border-medium)",
          borderRadius: 16,
          boxShadow: "0 8px 48px rgba(0,0,0,0.5)",
          width: "min(760px, calc(100vw - 32px))",
          maxHeight: "90vh",
          overflow: "auto",
        }}
      >
        <div
          style={{
            padding: "20px 24px 0",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
          }}
        >
          <h2 style={{ fontSize: 16, fontWeight: 700, margin: 0 }}>
            Create AKS Cluster
          </h2>
          <button
            onClick={onClose}
            style={{
              background: "none",
              border: "none",
              color: "var(--text-faint)",
              cursor: "pointer",
              padding: 4,
            }}
            title="Close"
          >
            <X size={18} />
          </button>
        </div>
        <div style={{ padding: "16px 24px 24px", display: "grid", gap: 16 }}>
          <div>
            <label
              style={{
                fontSize: 11,
                color: "var(--text-muted)",
                display: "block",
                marginBottom: 4,
              }}
            >
              Cluster Name
            </label>
            <input
              type="text"
              value={clusterName}
              onChange={(e) => setClusterName(e.target.value)}
              className="glass-input"
              style={{ width: "100%", fontSize: 13 }}
              placeholder="elb-cluster"
              autoFocus
            />
            {!clusterNameValid && clusterName.length > 0 && (
              <div style={{ fontSize: 10, color: "var(--danger)", marginTop: 4 }}>
                Must start with a letter, contain only letters/digits/hyphens, 2–63
                chars.
              </div>
            )}
          </div>

          <div>
            <div
              style={{
                fontSize: 11,
                fontWeight: 600,
                color: "var(--text-primary)",
                marginBottom: 8,
                textTransform: "uppercase",
                letterSpacing: 0.5,
              }}
            >
              Workload pool ·{" "}
              <span style={{ color: "var(--text-muted)", fontWeight: 400 }}>
                blastpool
              </span>
            </div>
            <div
              style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}
            >
              <div>
                <label
                  style={{
                    fontSize: 11,
                    color: "var(--text-muted)",
                    display: "block",
                    marginBottom: 4,
                  }}
                >
                  Node SKU
                </label>
                <select
                  value={nodeSku}
                  onChange={(e) => setNodeSku(e.target.value)}
                  className="glass-input"
                  style={{ width: "100%", fontSize: 13 }}
                >
                  {blastGroups.map((group) => (
                    <optgroup key={group.id} label={`── ${group.label} ──`}>
                      {group.skus.map((option) => (
                        <option key={option.name} value={option.name}>
                          {formatAksSkuOption(option)}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </select>
                <div className="muted" style={{ fontSize: 10, marginTop: 3 }}>
                  {describeAksSku(selectedSku)}
                </div>
              </div>
              <div>
                <label
                  style={{
                    fontSize: 11,
                    color: "var(--text-muted)",
                    display: "block",
                    marginBottom: 4,
                  }}
                >
                  Node Count
                </label>
                <input
                  type="number"
                  min={1}
                  max={100}
                  value={nodeCount}
                  onChange={(e) =>
                    setNodeCount(
                      Math.max(1, Math.min(100, parseInt(e.target.value) || 1)),
                    )
                  }
                  className="glass-input"
                  style={{ width: "100%", fontSize: 13 }}
                />
              </div>
            </div>
          </div>

          <div>
            <div
              style={{
                fontSize: 11,
                fontWeight: 600,
                color: "var(--text-primary)",
                marginBottom: 8,
                textTransform: "uppercase",
                letterSpacing: 0.5,
              }}
            >
              System pool ·{" "}
              <span style={{ color: "var(--text-muted)", fontWeight: 400 }}>
                systempool · CriticalAddonsOnly
              </span>
            </div>
            <div
              style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}
            >
              <div>
                <label
                  style={{
                    fontSize: 11,
                    color: "var(--text-muted)",
                    display: "block",
                    marginBottom: 4,
                  }}
                >
                  System VM size
                </label>
                <select
                  value={systemVmSize}
                  onChange={(e) => setSystemVmSize(e.target.value)}
                  className="glass-input"
                  style={{ width: "100%", fontSize: 13 }}
                >
                  {systemGroups.map((group) => (
                    <optgroup key={group.id} label={`── ${group.label} ──`}>
                      {group.skus.map((option) => (
                        <option key={option.name} value={option.name}>
                          {formatAksSkuOption(option)}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </select>
                <div className="muted" style={{ fontSize: 10, marginTop: 3 }}>
                  Hosts CoreDNS / metrics-server / CSI ·{" "}
                  {describeAksSku(selectedSystemSku)}
                </div>
              </div>
              <div>
                <label
                  style={{
                    fontSize: 11,
                    color: "var(--text-muted)",
                    display: "block",
                    marginBottom: 4,
                  }}
                >
                  System node count (1–{MAX_SYSTEM_NODE_COUNT})
                </label>
                <input
                  type="number"
                  min={1}
                  max={MAX_SYSTEM_NODE_COUNT}
                  value={systemNodeCount}
                  onChange={(e) =>
                    setSystemNodeCount(
                      Math.max(
                        1,
                        Math.min(
                          MAX_SYSTEM_NODE_COUNT,
                          parseInt(e.target.value) || 1,
                        ),
                      ),
                    )
                  }
                  className="glass-input"
                  style={{ width: "100%", fontSize: 13 }}
                />
              </div>
            </div>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
            <div>
              <label
                style={{
                  fontSize: 11,
                  color: "var(--text-muted)",
                  display: "block",
                  marginBottom: 4,
                }}
              >
                Region
              </label>
              <div
                style={{
                  fontSize: 13,
                  color: "var(--text-primary)",
                  padding: "6px 0",
                }}
              >
                {region || "Not set"}
              </div>
            </div>
            <div>
              <label
                style={{
                  fontSize: 11,
                  color: "var(--text-muted)",
                  display: "block",
                  marginBottom: 4,
                }}
              >
                Resource Group
              </label>
              <div
                style={{
                  fontSize: 13,
                  color: "var(--text-primary)",
                  padding: "6px 0",
                }}
              >
                {resourceGroup}
              </div>
            </div>
          </div>

          <div
            style={{
              padding: "10px 14px",
              background: "var(--glass-bg)",
              border: "1px solid var(--glass-border)",
              borderRadius: 8,
              fontSize: 12,
              color: "var(--text-muted)",
            }}
          >
            Est. cost:{" "}
            <strong style={{ color: "var(--text-primary)" }}>
              ~${estimatedCost.toFixed(2)}/hr
            </strong>
            <div style={{ fontSize: 11, marginTop: 4 }}>
              blastpool: {nodeCount} × {nodeSku} (~${blastCost.toFixed(2)}/hr) ·
              systempool: {systemNodeCount} × {systemVmSize} (~$
              {systemCost.toFixed(2)}/hr)
            </div>
            {!region && (
              <span style={{ color: "var(--danger)", marginLeft: 8 }}>
                Region required
              </span>
            )}
          </div>

          {provError && (
            <div
              style={{
                fontSize: 12,
                color: "var(--danger)",
                display: "flex",
                alignItems: "center",
                gap: 6,
              }}
            >
              <AlertTriangle size={12} /> {provError}
            </div>
          )}

          <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
            <button
              className="glass-button"
              onClick={onClose}
              style={{ fontSize: 12, padding: "8px 16px" }}
            >
              Cancel
            </button>
            <button
              className="glass-button glass-button--primary"
              onClick={onSubmit}
              disabled={provStatus === "creating" || !region || !clusterNameValid}
              style={{ fontSize: 12, padding: "8px 20px" }}
            >
              {provStatus === "creating" ? (
                <>
                  <Loader2 size={12} className="spin" /> Creating...
                </>
              ) : (
                <>
                  <Plus size={12} /> Create Cluster
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}
