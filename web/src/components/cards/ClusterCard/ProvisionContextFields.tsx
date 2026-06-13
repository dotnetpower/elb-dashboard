import type { ArmLocation } from "@/api/armProxy";
import { AZURE_REGIONS } from "@/constants";

import { panelChipStyle } from "./ProvisionPoolSections";
import { CLUSTER_TIER_OPTIONS } from "./useClusterProvisioning";
import type { ClusterTier } from "./useClusterProvisioning";

/**
 * Cluster identity fields (name + classification tier) of the AKS
 * {@link ProvisionModal}.
 *
 * Extracted verbatim from `ProvisionModal.tsx` (issue #24 SRP split): the
 * Cluster Name input with its validity hint and the optional classification
 * `tier` selector. Values + setters are threaded in via props so the render
 * output is byte-identical.
 */
export function ProvisionIdentityFields({
  clusterName,
  setClusterName,
  clusterNameValid,
  tier,
  setTier,
}: {
  clusterName: string;
  setClusterName: (v: string) => void;
  clusterNameValid: boolean;
  tier: ClusterTier;
  setTier: (v: ClusterTier) => void;
}) {
  return (
    <>
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
        <label
          htmlFor="provision-tier"
          style={{
            fontSize: 11,
            color: "var(--text-muted)",
            display: "block",
            marginBottom: 4,
          }}
        >
          Cluster classification (optional)
        </label>
        <select
          id="provision-tier"
          value={tier}
          onChange={(e) => setTier(e.target.value as ClusterTier)}
          className="glass-input"
          style={{ width: "100%", fontSize: 13 }}
        >
          {CLUSTER_TIER_OPTIONS.map((option) => (
            <option key={option.value || "_unset"} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <div className="muted" style={{ fontSize: 10, marginTop: 3 }}>
          Picking a tier pre-fills the workload SKU and node count below (e.g.{" "}
          <code>heavy</code> → E32s_v5 × 10) and writes the
          <code> elb-tier</code> ARM tag so the dashboard can group multi-cluster
          fleets at a glance. Edits you make to the SKU or node count after
          picking a tier are preserved.
        </div>
      </div>
    </>
  );
}

/**
 * Region + Resource Group context fields of the AKS {@link ProvisionModal}.
 *
 * Extracted verbatim from `ProvisionModal.tsx` (issue #24 SRP split): the
 * two-column grid holding the region selector (subscription-scoped with an
 * `AZURE_REGIONS` fallback) and the Resource Group input with its
 * validity / exists / cross-RG advisory notes. All values + setters are
 * threaded in via props so the render output is byte-identical.
 */
export function ProvisionContextFields({
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
}: {
  region: string;
  setRegion: (v: string) => void;
  availableLocations: ArmLocation[];
  locationsLoading: boolean;
  resourceGroup: string;
  setResourceGroup: (v: string) => void;
  resourceGroupValid: boolean;
  resourceGroupExists: boolean;
  resourceGroupsLoading: boolean;
  workloadResourceGroup?: string;
}) {
  return (
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
            borderColor: !resourceGroupValid ? "var(--danger)" : undefined,
          }}
          placeholder="rg-elb-cluster"
          spellCheck={false}
          autoComplete="off"
        />
        {!resourceGroupValid && resourceGroup.length > 0 && (
          <div style={{ fontSize: 10, color: "var(--danger)", marginTop: 3 }}>
            1–90 chars; letters, digits, <code>. _ - ( )</code> only; cannot end
            with a period.
          </div>
        )}
        {resourceGroupValid && resourceGroupExists && (
          <div className="muted" style={{ fontSize: 10, marginTop: 3 }}>
            Resource group{" "}
            <code style={{ ...panelChipStyle, padding: "0 4px" }}>
              {resourceGroup}
            </code>{" "}
            already exists — it will be reused. Multiple AKS clusters can share a
            single resource group.
          </div>
        )}
        {resourceGroupValid && !resourceGroupExists && (
          <div className="muted" style={{ fontSize: 10, marginTop: 3 }}>
            {resourceGroupsLoading
              ? "Checking existing resource groups…"
              : "New resource group will be created."}
          </div>
        )}
        {resourceGroupValid &&
          workloadResourceGroup &&
          resourceGroup !== workloadResourceGroup && (
            <div
              style={{
                fontSize: 10,
                color: "var(--warning, var(--text-muted))",
                marginTop: 3,
              }}
            >
              Cluster will land in{" "}
              <code style={{ ...panelChipStyle, padding: "0 4px" }}>
                {resourceGroup}
              </code>
              , not the dashboard's Workload RG{" "}
              <code style={{ ...panelChipStyle, padding: "0 4px" }}>
                {workloadResourceGroup}
              </code>
              . The card will keep tracking it during provisioning; to manage it
              later, switch the dashboard's Workload RG to{" "}
              <code style={{ ...panelChipStyle, padding: "0 4px" }}>
                {resourceGroup}
              </code>
              .
            </div>
          )}
      </div>
    </div>
  );
}
