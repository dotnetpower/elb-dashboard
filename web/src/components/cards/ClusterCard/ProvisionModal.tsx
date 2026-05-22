import { useEffect, useRef, useState } from "react";
import type { CSSProperties, MouseEvent as ReactMouseEvent } from "react";
import { createPortal } from "react-dom";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Cpu,
  Loader2,
  Plus,
  Settings2,
  X,
} from "lucide-react";

import type { AksSku } from "@/api/endpoints";
import type { ArmLocation } from "@/api/armProxy";
import { AZURE_REGIONS } from "@/constants";
import {
  describeAksSku,
  formatAksSkuOption,
  groupAksSkus,
} from "@/hooks/useAksSkus";
import { useFocusTrap } from "@/hooks/useFocusTrap";

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
  // context (editable)
  region,
  setRegion,
  availableLocations,
  locationsLoading,
  resourceGroup,
  setResourceGroup,
  resourceGroupValid,
  resourceGroupConflict,
  resourceGroupsLoading,
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
  region: string;
  setRegion: (v: string) => void;
  availableLocations: ArmLocation[];
  locationsLoading: boolean;
  resourceGroup: string;
  setResourceGroup: (v: string) => void;
  resourceGroupValid: boolean;
  resourceGroupConflict: boolean;
  resourceGroupsLoading: boolean;
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

  // System pool is collapsed by default — 95% of users keep the defaults
  // (D-series × 1 node), and showing the full panel inflates the modal.
  // Auto-expand if the user has already tweaked the defaults so they don't
  // get hidden behind a toggle when reopening the modal.
  const systemUsesDefaults =
    systemVmSize === "Standard_D2s_v3" && systemNodeCount === 1;
  const [systemPoolExpanded, setSystemPoolExpanded] = useState(
    !systemUsesDefaults,
  );

  // Trap keyboard focus inside the dialog while it is open so Tab cycles
  // through the form fields and never escapes to the dashboard behind.
  const trapRef = useFocusTrap<HTMLDivElement>(true);

  // Snapshot the form values at mount time. A backdrop click is treated as
  // an accidental dismiss when the user has changed something — confirm
  // before throwing away their input. ESC and Cancel close immediately;
  // both are deliberate, keyboard/mouse-targeted actions.
  const initialSnapshot = useRef({
    clusterName,
    nodeSku,
    nodeCount,
    systemVmSize,
    systemNodeCount,
    region,
    resourceGroup,
  });
  const isDirty =
    clusterName !== initialSnapshot.current.clusterName ||
    nodeSku !== initialSnapshot.current.nodeSku ||
    nodeCount !== initialSnapshot.current.nodeCount ||
    systemVmSize !== initialSnapshot.current.systemVmSize ||
    systemNodeCount !== initialSnapshot.current.systemNodeCount ||
    region !== initialSnapshot.current.region ||
    resourceGroup !== initialSnapshot.current.resourceGroup;
  const handleBackdropClick = (e: ReactMouseEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return;
    if (
      isDirty &&
      !window.confirm("Discard changes and close this dialog?")
    ) {
      return;
    }
    onClose();
  };

  const selectedSku = skuOptions.find((option) => option.name === nodeSku);
  const selectedSystemSku = skuOptions.find(
    (option) => option.name === systemVmSize,
  );
  // Only compute cost when we actually have a price from the catalog —
  // otherwise the footer shows "—" instead of a confident-looking but
  // fabricated number. (`hourlyUsd <= 0` happens for region-locked SKUs.)
  const blastHourly = selectedSku?.hourlyUsd ?? 0;
  const systemHourly = selectedSystemSku?.hourlyUsd ?? 0;
  const hasBlastPrice = blastHourly > 0;
  const hasSystemPrice = systemHourly > 0;
  const blastCost = blastHourly * nodeCount;
  const systemCost = systemHourly * systemNodeCount;
  const estimatedCost = blastCost + systemCost;
  const hasFullCost = hasBlastPrice && hasSystemPrice;
  // 730 h/mo (365.25 × 24 / 12) is the standard Azure billing reference
  // used by the pricing calculator — matches what the user will see on
  // their invoice within a few cents.
  const HOURS_PER_MONTH = 730;

  const blastGroups = groupAksSkus(skuOptions, "blast", groupOrder, groupLabels);
  const systemGroups = groupAksSkus(skuOptions, "system", groupOrder, groupLabels);

  // Shared styles for the two pool panels. Each pool gets its own colour
  // accent so the modal reads less like a grey form: workload pool = cool
  // blue (active compute), system pool = muted neutral (quiet housekeeping).
  // Accent comes through a left border stripe + a faint linear-gradient
  // tint on top of the glass surface.
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
  const panelChipStyle: CSSProperties = {
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

  return createPortal(
    <div
      className="glass-dialog-backdrop provision-modal-backdrop"
      onClick={handleBackdropClick}
      role="dialog"
      aria-modal="true"
      aria-label="Create AKS Cluster"
    >
      <div
        ref={trapRef}
        className="glass-card glass-card--strong glass-dialog provision-modal-card"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "min(880px, calc(100vw - 32px))",
          maxWidth: "min(880px, calc(100vw - 32px))",
          maxHeight: "90vh",
          // Card itself does not scroll — only the body wrapper below does,
          // so the header (title) and footer (Cancel/Create) stay pinned.
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
          textAlign: "left",
          padding: 0,
        }}
      >
        <div
          style={{
            padding: "var(--space-4) var(--space-5) var(--space-3)",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            borderBottom: "1px solid var(--glass-border)",
            flex: "0 0 auto",
          }}
        >
          <h3 style={{ margin: 0, display: "flex", alignItems: "center", gap: 8 }}>
            <Plus size={18} strokeWidth={1.5} /> Create AKS Cluster
          </h3>
          <button
            className="glass-button"
            onClick={onClose}
            style={{ padding: "4px 6px", border: "none" }}
            title="Close"
          >
            <X size={16} strokeWidth={1.5} />
          </button>
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            // Mirror the Create button's disabled conditions so an Enter
            // press in any field can't bypass validation.
            if (
              provStatus === "creating" ||
              !region ||
              !clusterNameValid ||
              !resourceGroupValid ||
              resourceGroupConflict
            ) {
              return;
            }
            onSubmit();
          }}
          style={{
            flex: "1 1 auto",
            display: "flex",
            flexDirection: "column",
            minHeight: 0,
          }}
        >
        <div
          style={{
            // Scrollable body — everything between header and footer.
            flex: "1 1 auto",
            overflowY: "auto",
            // Stop wheel/touch scroll from chaining to the dashboard behind
            // the modal once we hit the top/bottom of this scroller.
            overscrollBehavior: "contain",
            padding: "var(--space-4) var(--space-5)",
            display: "grid",
            gap: "var(--space-4)",
          }}
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
              <code style={{ ...panelChipStyle, padding: "0 4px" }}>core_nt</code>,
              and raise <strong>Node Count</strong> to finish faster — the
              database is sharded across nodes, so search time scales down
              roughly linearly with the number of nodes (2× nodes ≈ half the
              wall-clock time).
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
                    setNodeCount(
                      Math.max(1, Math.min(100, parseInt(e.target.value) || 1)),
                    )
                  }
                  className="glass-input"
                  style={{ width: "100%", fontSize: 13 }}
                />
              </div>
            </div>
          </section>

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
                    <span style={{ color: "var(--text-faint)" }}>
                      {" "}
                      (defaults)
                    </span>
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
                  A small dedicated pool that runs the cluster's own
                  housekeeping — name lookups, health checks, disk drivers.
                  Keeping it separate means a heavy BLAST search never slows
                  the cluster itself down. One small machine is usually enough.
                  <br />
                  The{" "}
                  <code style={{ ...panelChipStyle, padding: "0 4px" }}>
                    CriticalAddonsOnly
                  </code>{" "}
                  tag tells Kubernetes:{" "}
                  <em>only system tasks may run here</em>.
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
                        <optgroup
                          key={group.id}
                          label={`── ${group.label} ──`}
                        >
                          {group.skus.map((option) => (
                            <option key={option.name} value={option.name}>
                              {formatAksSkuOption(option)}
                            </option>
                          ))}
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
            )}
          </section>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
            <div>
              <label
                htmlFor="provision-region"
                style={{
                  fontSize: 11,
                  color: "var(--text-muted)",
                  display: "block",
                  marginBottom: 4,
                }}
              >
                Region
              </label>
              {(() => {
                // Prefer the subscription's actual location list (so compliance-
                // restricted subs see only what they can deploy to); fall back to
                // the bundled AZURE_REGIONS while the query is loading or if the
                // backend returns an empty list. The selected `region` value is
                // always present in the dropdown, even if it isn't in either
                // source, via the "(current)" option.
                const useSubscriptionList = availableLocations.length > 0;
                const sourceOptions = useSubscriptionList
                  ? availableLocations.map((l) => ({
                      value: l.name,
                      label: l.regionalDisplayName || l.displayName,
                    }))
                  : AZURE_REGIONS.map((r) => ({ value: r.value, label: r.label }));
                const regionInSource = region
                  ? sourceOptions.some((o) => o.value === region)
                  : false;
                return (
                  <>
                    <select
                      id="provision-region"
                      value={region}
                      onChange={(e) => setRegion(e.target.value)}
                      className="glass-input"
                      style={{ width: "100%", fontSize: 13 }}
                    >
                      {!region && <option value="">Select a region…</option>}
                      {region && !regionInSource && (
                        <option value={region}>{region} (current)</option>
                      )}
                      {sourceOptions.map((r) => (
                        <option key={r.value} value={r.value}>
                          {r.label}
                        </option>
                      ))}
                    </select>
                    <div className="muted" style={{ fontSize: 10, marginTop: 3 }}>
                      {locationsLoading
                        ? "Loading subscription regions…"
                        : useSubscriptionList
                          ? "From this subscription's allowed locations."
                          : "Default region list (subscription list unavailable)."}
                    </div>
                  </>
                );
              })()}
            </div>
            <div>
              <label
                htmlFor="provision-rg"
                style={{
                  fontSize: 11,
                  color: "var(--text-muted)",
                  display: "block",
                  marginBottom: 4,
                }}
              >
                Resource Group
              </label>
              <input
                id="provision-rg"
                type="text"
                value={resourceGroup}
                onChange={(e) => setResourceGroup(e.target.value)}
                className="glass-input"
                style={{
                  width: "100%",
                  fontSize: 13,
                  borderColor: !resourceGroupValid
                    ? "var(--danger)"
                    : resourceGroupConflict
                      ? "var(--warning)"
                      : undefined,
                }}
                placeholder="rg-elb-cluster"
                spellCheck={false}
                autoComplete="off"
              />
              {!resourceGroupValid && resourceGroup.length > 0 && (
                <div style={{ fontSize: 10, color: "var(--danger)", marginTop: 3 }}>
                  1–90 chars; letters, digits, <code>. _ - ( )</code> only;
                  cannot end with a period.
                </div>
              )}
              {resourceGroupValid && resourceGroupConflict && (
                <div style={{ fontSize: 10, color: "var(--warning)", marginTop: 3 }}>
                  A resource group named{" "}
                  <code style={{ ...panelChipStyle, padding: "0 4px" }}>
                    {resourceGroup}
                  </code>{" "}
                  already exists in this subscription. Pick a different name.
                </div>
              )}
              {resourceGroupValid && !resourceGroupConflict && (
                <div className="muted" style={{ fontSize: 10, marginTop: 3 }}>
                  {resourceGroupsLoading
                    ? "Checking existing resource groups…"
                    : "Name is available."}
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Sticky footer — always visible so the Create button never gets
            hidden under the scroll fold. Cost (left) + actions (right);
            errors stack above the row. */}
        <div
          style={{
            flex: "0 0 auto",
            padding: "var(--space-3) var(--space-5)",
            borderTop: "1px solid var(--glass-border)",
            background: "var(--bg-secondary)",
            display: "flex",
            flexDirection: "column",
            gap: 8,
          }}
        >
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
              <AlertTriangle size={12} strokeWidth={1.5} /> {provError}
            </div>
          )}

          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 16,
              flexWrap: "wrap",
            }}
          >
            {/* Cost block — left, prominent so users see budget impact
                while tweaking node count above. */}
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              <div
                style={{
                  fontSize: 10,
                  color: "var(--text-muted)",
                  textTransform: "uppercase",
                  letterSpacing: 0.5,
                }}
              >
                Estimated cost
              </div>
              <div
                style={{
                  fontSize: 16,
                  fontWeight: 600,
                  color: "var(--text-primary)",
                  lineHeight: 1.2,
                }}
              >
                {hasFullCost ? (
                  <>
                    ${estimatedCost.toFixed(2)}/hr
                    <span
                      style={{
                        fontSize: 12,
                        fontWeight: 400,
                        color: "var(--text-muted)",
                        marginLeft: 8,
                      }}
                    >
                      ≈ ${(estimatedCost * HOURS_PER_MONTH).toFixed(0)}/mo
                    </span>
                  </>
                ) : (
                  <span style={{ color: "var(--text-muted)", fontWeight: 400 }}>
                    — price unavailable
                  </span>
                )}
              </div>
              <div style={{ fontSize: 10, color: "var(--text-faint)" }}>
                blastpool {nodeCount} × {nodeSku} · systempool {systemNodeCount}{" "}
                × {systemVmSize}
              </div>
            </div>

            <div style={{ display: "flex", gap: 10 }}>
              <button
                type="button"
                className="glass-button"
                onClick={onClose}
                style={{ fontSize: 12, padding: "8px 16px" }}
              >
                Cancel
              </button>
              <button
                type="submit"
                className="glass-button glass-button--primary"
                disabled={
                  provStatus === "creating" ||
                  !region ||
                  !clusterNameValid ||
                  !resourceGroupValid ||
                  resourceGroupConflict
                }
                style={{ fontSize: 12, padding: "8px 20px" }}
              >
                {provStatus === "creating" ? (
                  <>
                    <Loader2 size={12} strokeWidth={1.5} className="spin" />{" "}
                    Creating...
                  </>
                ) : (
                  <>
                    <Plus size={12} strokeWidth={1.5} /> Create Cluster
                  </>
                )}
              </button>
            </div>
          </div>
        </div>
        </form>
      </div>
    </div>,
    document.body,
  );
}
