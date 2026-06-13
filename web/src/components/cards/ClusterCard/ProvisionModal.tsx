import { useEffect, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import { createPortal } from "react-dom";
import { AlertCircle, Loader2, Plus, X } from "lucide-react";

import type { AksPreflightResponse, AksSku } from "@/api/endpoints";
import type { ArmLocation } from "@/api/armProxy";
import {
  DEFAULT_AKS_SYSTEM_NODE_COUNT,
  DEFAULT_AKS_SYSTEM_SKU,
  groupAksSkus,
} from "@/hooks/useAksSkus";
import { useFocusTrap } from "@/hooks/useFocusTrap";

import {
  ProvisionContextFields,
  ProvisionIdentityFields,
} from "./ProvisionContextFields";
import { ProvisionErrorCard } from "./ProvisionErrorCard";
import { PreflightChecklist } from "./PreflightChecklist";
import { WorkloadPoolSection, SystemPoolSection } from "./ProvisionPoolSections";
import { ProvisioningStatusPanel } from "./ProvisioningStatusPanel";
import type { ProvisionProgress } from "./ProvisioningBanner";
import type { ClusterTier } from "./useClusterProvisioning";

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
  tier,
  setTier,
  // sku catalog
  skuOptions,
  groupLabels,
  groupOrder,
  // sku availability (region-filtered)
  availableSkusSet,
  unavailableSkusMap,
  availabilityLoading,
  availabilityDegraded,
  // context (editable)
  region,
  setRegion,
  availableLocations,
  locationsLoading,
  resourceGroup,
  setResourceGroup,
  resourceGroupValid,
  resourceGroupExists,
  resourceGroupsLoading,
  workloadResourceGroup,
  // preflight
  preflightStatus,
  preflightResult,
  // live provisioning state
  taskPhase,
  taskProgress,
  elapsed,
  // identity context (read-only)
  subscriptionId,
  // status
  provStatus,
  provError,
  // actions
  onSubmit,
  onClose,
  onErrorReset,
  onCancel,
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
  /** Free-form cluster classification — written to ARM as the
   *  `elb-tier` tag. Empty string == leave the tag off (the default). */
  tier: ClusterTier;
  setTier: (v: ClusterTier) => void;
  skuOptions: AksSku[];
  groupLabels: Record<string, string>;
  groupOrder: string[];
  /** Set of SKU names actually deployable in the chosen `region` for
   *  the current subscription. SKUs not in this set are still listed
   *  in the dropdown but rendered as `disabled` so the user can see
   *  *why* their preferred SKU is unavailable. */
  availableSkusSet: Set<string>;
  /** SKU name → human reason (e.g. `NotAvailableForSubscription`) for
   *  every disabled SKU. Used as the `<option>` title attribute. */
  unavailableSkusMap: Map<string, string>;
  /** `availableSkusSet` is still loading from `/api/aks/available-skus`. */
  availabilityLoading: boolean;
  /** Azure listing failed entirely — the SPA shows everything (so the
   *  user is not blocked by a backend outage) and surfaces a small
   *  hint instead. */
  availabilityDegraded: boolean;
  region: string;
  setRegion: (v: string) => void;
  availableLocations: ArmLocation[];
  locationsLoading: boolean;
  resourceGroup: string;
  setResourceGroup: (v: string) => void;
  resourceGroupValid: boolean;
  /** True when an RG with the typed name already exists in the
   *  subscription. **Informational only** — reusing an existing RG
   *  for additional AKS clusters is supported (a single RG can host
   *  multiple clusters, and `provision_aks` is idempotent on RG
   *  ensure). The modal renders a neutral "will reuse" note when
   *  this is true. */
  resourceGroupExists: boolean;
  resourceGroupsLoading: boolean;
  /** The dashboard's workload Resource Group (the one the card is
   *  currently listing clusters for). When this differs from the
   *  modal's `resourceGroup`, the new cluster will land in a RG that
   *  the card is not currently scoped to. The modal surfaces a soft
   *  inline note so the user understands why they may need to switch
   *  the dashboard's Workload RG later to manage the cluster. */
  workloadResourceGroup?: string;
  /** "idle" before the user has clicked Create; "checking" while the
   *  preflight HTTP call is in flight; "done" once results are in. */
  preflightStatus: "idle" | "checking" | "done";
  /** Result payload from the latest `/api/aks/preflight` call. */
  preflightResult: AksPreflightResponse | null;
  /** Celery phase string (e.g. `arm_create_or_update`) once the
   *  provision task starts publishing progress. The modal renders a
   *  compact "live provisioning" panel that mirrors the dashboard
   *  banner while the modal is still open. */
  taskPhase: string | null;
  /** Rich progress payload published by `provision_aks`. Carries step
   *  counters, pool states, ARM elapsed seconds and the Azure portal
   *  deep link once the cluster is visible. */
  taskProgress: ProvisionProgress | null;
  /** Seconds since `handleProvision` was called. Mirrors the elapsed
   *  counter the dashboard banner uses, kept here so the modal can
   *  show the same live counter without re-deriving it. */
  elapsed: number;
  /** Subscription id the modal is operating against. Used only to
   *  scope the Azure portal deep links the error card surfaces
   *  (quota blade, RG overview). */
  subscriptionId: string;
  provStatus: string;
  provError: string | null;
  onSubmit: () => void;
  onClose: () => void;
  /** Clear the provisioning error so the form becomes editable again
   *  (used by the error card's Dismiss + Edit-&-retry buttons). */
  onErrorReset: () => void;
  /** Cancel the in-flight provisioning task. Surfaced as a "Stop"
   *  button on the live progress panel; omitted (button hidden) when
   *  the parent doesn't wire one. */
  onCancel?: () => Promise<void> | void;
}) {
  // ESC to close. Mirrors the backdrop-click confirm so an accidental
  // ESC during provisioning doesn't yank the live progress panel out
  // from under the user.
  useEffect(() => {
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (provStatus === "creating") {
        if (
          !window.confirm(
            "Provisioning is still running in the background. Close this dialog?",
          )
        ) {
          return;
        }
      }
      onClose();
    };
    window.addEventListener("keydown", handleEsc);
    return () => window.removeEventListener("keydown", handleEsc);
  }, [onClose, provStatus]);

  // System pool is collapsed by default — 95% of users keep the defaults
  // (D-series × default node count), and showing the full panel inflates the modal.
  // Auto-expand if the user has already tweaked the defaults so they don't
  // get hidden behind a toggle when reopening the modal.
  const systemUsesDefaults =
    systemVmSize === DEFAULT_AKS_SYSTEM_SKU &&
    systemNodeCount === DEFAULT_AKS_SYSTEM_NODE_COUNT;
  const [systemPoolExpanded, setSystemPoolExpanded] = useState(
    !systemUsesDefaults,
  );
  const [showPreflightChecking, setShowPreflightChecking] = useState(false);

  useEffect(() => {
    if (preflightStatus !== "checking") {
      setShowPreflightChecking(false);
      return;
    }
    const timer = window.setTimeout(() => setShowPreflightChecking(true), 300);
    return () => window.clearTimeout(timer);
  }, [preflightStatus]);

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
    // While the Celery task is in flight the modal carries live
    // progress that the user may need to see; confirm before throwing
    // it away. The dashboard banner will still pick up the long tail.
    if (provStatus === "creating") {
      if (
        !window.confirm(
          "Provisioning is still running in the background. Close this dialog?",
        )
      ) {
        return;
      }
    } else if (
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
          height: "min(760px, 90vh)",
          maxHeight: "min(760px, 90vh)",
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
              preflightStatus === "checking" ||
              !region ||
              !clusterNameValid ||
              !resourceGroupValid ||
              preflightResult?.ok === false
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
          <ProvisionIdentityFields
            clusterName={clusterName}
            setClusterName={setClusterName}
            clusterNameValid={clusterNameValid}
            tier={tier}
            setTier={setTier}
          />

          <WorkloadPoolSection
            blastGroups={blastGroups}
            availableSkusSet={availableSkusSet}
            unavailableSkusMap={unavailableSkusMap}
            region={region}
            nodeSku={nodeSku}
            setNodeSku={setNodeSku}
            nodeCount={nodeCount}
            setNodeCount={setNodeCount}
            selectedSku={selectedSku}
            availabilityLoading={availabilityLoading}
            availabilityDegraded={availabilityDegraded}
          />

          <SystemPoolSection
            systemGroups={systemGroups}
            availableSkusSet={availableSkusSet}
            unavailableSkusMap={unavailableSkusMap}
            region={region}
            systemVmSize={systemVmSize}
            setSystemVmSize={setSystemVmSize}
            systemNodeCount={systemNodeCount}
            setSystemNodeCount={setSystemNodeCount}
            selectedSystemSku={selectedSystemSku}
            systemPoolExpanded={systemPoolExpanded}
            setSystemPoolExpanded={setSystemPoolExpanded}
            systemUsesDefaults={systemUsesDefaults}
          />

          <ProvisionContextFields
            region={region}
            setRegion={setRegion}
            availableLocations={availableLocations}
            locationsLoading={locationsLoading}
            resourceGroup={resourceGroup}
            setResourceGroup={setResourceGroup}
            resourceGroupValid={resourceGroupValid}
            resourceGroupExists={resourceGroupExists}
            resourceGroupsLoading={resourceGroupsLoading}
            workloadResourceGroup={workloadResourceGroup}
          />
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
          <PreflightChecklist
            preflightStatus={preflightStatus}
            showPreflightChecking={showPreflightChecking}
            preflightResult={preflightResult}
            nodeCount={nodeCount}
            setNodeCount={setNodeCount}
          />

          <ProvisioningStatusPanel
            provStatus={provStatus}
            taskProgress={taskProgress}
            taskPhase={taskPhase}
            elapsed={elapsed}
            onCancel={onCancel}
          />

          {(provStatus === "error" || provError) && (
            <ProvisionErrorCard
              raw={provError ?? "Provisioning failed."}
              context={{
                subscriptionId,
                region,
                resourceGroup,
              }}
              // R-3: if cancellation landed after the cluster ARM
              // resource was already visible, surface a direct
              // portal link so the user can verify/delete the
              // partial cluster from the modal too.
              extraPortalUrl={
                (provError ?? "").includes("cancelled") &&
                typeof taskProgress?.portal_url === "string"
                  ? (taskProgress?.portal_url as string)
                  : undefined
              }
              onDismiss={onErrorReset}
              onRetry={onErrorReset}
            />
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
                  preflightStatus === "checking" ||
                  !region ||
                  !clusterNameValid ||
                  !resourceGroupValid ||
                  // A `fail` row in the most recent preflight blocks
                  // submit until the user changes inputs (which clears
                  // the preflight result back to idle).
                  preflightResult?.ok === false
                }
                style={{ fontSize: 12, padding: "8px 20px" }}
              >
                {provStatus === "creating" ? (
                  <>
                    <Loader2 size={12} strokeWidth={1.5} className="spin" />{" "}
                    Creating...
                  </>
                ) : preflightStatus === "checking" && showPreflightChecking ? (
                  <>
                    <Loader2 size={12} strokeWidth={1.5} className="spin" />{" "}
                    Validating...
                  </>
                ) : preflightResult?.ok === false ? (
                  <>
                    <AlertCircle size={12} strokeWidth={1.5} /> Fix errors above
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
