import type { CSSProperties } from "react";
import { ChevronDown, ChevronRight, Cpu, Settings2 } from "lucide-react";

import type { AksSku } from "@/api/endpoints";
import {
  describeAksSku,
  formatAksSkuOption,
  type AksSkuGroup,
} from "@/hooks/useAksSkus";

import { MAX_SYSTEM_NODE_COUNT } from "./useClusterProvisioning";

/**
 * Workload + System node-pool form sections of the AKS {@link ProvisionModal}.
 *
 * Extracted verbatim from `ProvisionModal.tsx` (issue #24 SRP split): the two
 * `<section>` panels plus their shared glass-panel style tokens. All form state
 * lives in the modal's parent hook and is threaded in via props, so the render
 * output is byte-identical to the pre-split modal. `panelChipStyle` is exported
 * because the modal's Resource Group block reuses the same chip token.
 */

// Shared styles for the two pool panels. Each pool gets its own colour accent
// so the modal reads less like a grey form: workload pool = cool blue (active
// compute), system pool = muted neutral (quiet housekeeping). Accent comes
// through a left border stripe + a faint linear-gradient tint on top of the
// glass surface.
const makePanelStyle = (accent: string, tint: string): CSSProperties => ({
  position: "relative",
  background: `linear-gradient(135deg, ${tint} 0%, var(--glass-bg) 60%)`,
  border: "1px solid var(--glass-border)",
  borderLeft: `3px solid ${accent}`,
  borderRadius: 10,
  padding: "var(--space-3) var(--space-4)",
});
const workloadPanelStyle = makePanelStyle(
  "var(--accent)",
  "rgba(110, 159, 255, 0.10)", // matches --accent #6e9fff at ~10% alpha
);
// System pool gets a muted teal accent — distinct from the workload pool's
// blue, but still cool/calm (charter: no saturated brand colours). Picks
// hint "infrastructure" without screaming for attention.
const systemPanelStyle = makePanelStyle(
  "rgba(122, 197, 201, 0.65)", // muted teal #7ac5c9
  "rgba(122, 197, 201, 0.10)",
);
const panelHeaderStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  flexWrap: "wrap",
  gap: 8,
  marginBottom: 4,
};
const panelTitleStyle: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  fontSize: 12,
  fontWeight: 600,
  color: "var(--text-primary)",
  letterSpacing: 0.2,
};
export const panelChipStyle: CSSProperties = {
  fontSize: 10,
  fontFamily: "var(--font-mono, monospace)",
  color: "var(--text-muted)",
  background: "var(--bg-secondary)",
  border: "1px solid var(--border-weak)",
  borderRadius: 4,
  padding: "1px 6px",
  // Explicit: never uppercase chip text (CriticalAddonsOnly etc. keep case).
  textTransform: "none",
  letterSpacing: 0,
};
const panelHelpStyle: CSSProperties = {
  margin: "0 0 var(--space-3)",
  fontSize: 11.5,
  color: "var(--text-muted)",
  lineHeight: 1.55,
};
const panelGridStyle: CSSProperties = {
  display: "grid",
  // SKU dropdown takes the rest; Node Count is just a number — keep it
  // narrow so the modal doesn't waste horizontal space on a 3-digit field.
  // 160px is wide enough to also fit the System pool's longer
  // "System node count (1–3)" label without wrapping.
  gridTemplateColumns: "minmax(0, 1fr) 160px",
  gap: 16,
};

export function WorkloadPoolSection({
  blastGroups,
  availableSkusSet,
  unavailableSkusMap,
  region,
  nodeSku,
  setNodeSku,
  nodeCount,
  setNodeCount,
  selectedSku,
  availabilityLoading,
  availabilityDegraded,
}: {
  blastGroups: AksSkuGroup[];
  availableSkusSet: Set<string>;
  unavailableSkusMap: Map<string, string>;
  region: string;
  nodeSku: string;
  setNodeSku: (v: string) => void;
  nodeCount: number;
  setNodeCount: (v: number) => void;
  selectedSku: AksSku | undefined;
  availabilityLoading: boolean;
  availabilityDegraded: boolean;
}) {
  return (
    <section style={workloadPanelStyle}>
      <header style={panelHeaderStyle}>
        <span style={panelTitleStyle}>
          <Cpu size={14} strokeWidth={1.5} /> Workload pool
        </span>
        <code style={panelChipStyle}>blastpool</code>
      </header>
      <p style={panelHelpStyle}>
        Where your BLAST searches actually run. Pick a SKU with{" "}
        <strong>more memory</strong> for large databases like{" "}
        <code style={{ ...panelChipStyle, padding: "0 4px" }}>core_nt</code>, and
        raise <strong>Node Count</strong> to finish faster — the database is
        sharded across nodes, so search time scales down roughly linearly with
        the number of nodes (2× nodes ≈ half the wall-clock time).
      </p>
      <div style={panelGridStyle}>
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
                {group.skus.map((option) => {
                  const blocked = !availableSkusSet.has(option.name);
                  const reason = unavailableSkusMap.get(option.name);
                  return (
                    <option
                      key={option.name}
                      value={option.name}
                      disabled={blocked}
                      title={
                        blocked
                          ? `Not available in ${region || "this region"}${
                              reason ? ` · ${reason}` : ""
                            }`
                          : undefined
                      }
                    >
                      {formatAksSkuOption(option)}
                      {blocked ? ` — not available in ${region || "region"}` : ""}
                    </option>
                  );
                })}
              </optgroup>
            ))}
          </select>
          <div className="muted" style={{ fontSize: 10, marginTop: 3 }}>
            {describeAksSku(selectedSku)}
            {selectedSku && nodeCount > 0
              ? ` · ${nodeCount} × ${selectedSku.vCPUs} = ${nodeCount * selectedSku.vCPUs} cores total`
              : ""}
            {availabilityLoading && region
              ? ` · Checking availability in ${region}…`
              : ""}
            {availabilityDegraded
              ? " · Could not verify region availability; pre-flight will catch any issues."
              : ""}
          </div>
        </div>
        <div>
          <label
            style={{
              fontSize: 11,
              color: "var(--text-muted)",
              display: "block",
              marginBottom: 4,
              whiteSpace: "nowrap",
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
              setNodeCount(Math.max(1, Math.min(100, parseInt(e.target.value) || 1)))
            }
            className="glass-input"
            style={{ width: "100%", fontSize: 13 }}
          />
        </div>
      </div>
    </section>
  );
}

export function SystemPoolSection({
  systemGroups,
  availableSkusSet,
  unavailableSkusMap,
  region,
  systemVmSize,
  setSystemVmSize,
  systemNodeCount,
  setSystemNodeCount,
  selectedSystemSku,
  systemPoolExpanded,
  setSystemPoolExpanded,
  systemUsesDefaults,
}: {
  systemGroups: AksSkuGroup[];
  availableSkusSet: Set<string>;
  unavailableSkusMap: Map<string, string>;
  region: string;
  systemVmSize: string;
  setSystemVmSize: (v: string) => void;
  systemNodeCount: number;
  setSystemNodeCount: (v: number) => void;
  selectedSystemSku: AksSku | undefined;
  systemPoolExpanded: boolean;
  setSystemPoolExpanded: React.Dispatch<React.SetStateAction<boolean>>;
  systemUsesDefaults: boolean;
}) {
  return (
    <section style={systemPanelStyle}>
      <button
        type="button"
        style={{
          ...panelHeaderStyle,
          cursor: "pointer",
          marginBottom: systemPoolExpanded ? 4 : 0,
          // Reset native <button> styles so the row still reads as a
          // panel header rather than a chunky form button.
          background: "transparent",
          border: "none",
          padding: 0,
          color: "inherit",
          font: "inherit",
          textAlign: "left",
          width: "100%",
        }}
        aria-expanded={systemPoolExpanded}
        aria-controls="system-pool-body"
        onClick={() => setSystemPoolExpanded((v) => !v)}
      >
        <span style={panelTitleStyle}>
          <Settings2 size={14} strokeWidth={1.5} /> System pool
        </span>
        <code style={panelChipStyle}>systempool</code>
        <code style={panelChipStyle}>CriticalAddonsOnly</code>
        {!systemPoolExpanded && (
          <span
            style={{
              fontSize: 11,
              color: "var(--text-muted)",
              marginLeft: 4,
            }}
          >
            · {systemVmSize} × {systemNodeCount}
            {systemUsesDefaults && (
              <span style={{ color: "var(--text-faint)" }}> (defaults)</span>
            )}
          </span>
        )}
        <span
          style={{
            marginLeft: "auto",
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
            fontSize: 11,
            color: "var(--text-muted)",
          }}
        >
          {systemPoolExpanded ? "Hide" : "Advanced"}
          {systemPoolExpanded ? (
            <ChevronDown size={14} strokeWidth={1.5} />
          ) : (
            <ChevronRight size={14} strokeWidth={1.5} />
          )}
        </span>
      </button>
      {systemPoolExpanded && (
        <div id="system-pool-body">
          <p style={panelHelpStyle}>
            A small dedicated pool that runs the cluster's own housekeeping —
            name lookups, health checks, disk drivers. Keeping it separate means
            a heavy BLAST search never slows the cluster itself down. One small
            machine is usually enough.
            <br />
            The{" "}
            <code style={{ ...panelChipStyle, padding: "0 4px" }}>
              CriticalAddonsOnly
            </code>{" "}
            tag tells Kubernetes: <em>only system tasks may run here</em>.
          </p>
          <div style={panelGridStyle}>
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
                    {group.skus.map((option) => {
                      const blocked = !availableSkusSet.has(option.name);
                      const reason = unavailableSkusMap.get(option.name);
                      return (
                        <option
                          key={option.name}
                          value={option.name}
                          disabled={blocked}
                          title={
                            blocked
                              ? `Not available in ${region || "this region"}${
                                  reason ? ` · ${reason}` : ""
                                }`
                              : undefined
                          }
                        >
                          {formatAksSkuOption(option)}
                          {blocked
                            ? ` — not available in ${region || "region"}`
                            : ""}
                        </option>
                      );
                    })}
                  </optgroup>
                ))}
              </select>
              <div className="muted" style={{ fontSize: 10, marginTop: 3 }}>
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
                  whiteSpace: "nowrap",
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
                      Math.min(MAX_SYSTEM_NODE_COUNT, parseInt(e.target.value) || 1),
                    ),
                  )
                }
                className="glass-input"
                style={{ width: "100%", fontSize: 13 }}
              />
              <div className="muted" style={{ fontSize: 10, marginTop: 3 }}>
                Default 1 is enough for dev / test. Bump to 2+ for higher
                availability of CoreDNS / metrics-server in production-grade
                clusters.
              </div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
