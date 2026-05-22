import { Loader2, MapPin } from "lucide-react";
import type { UseQueryResult } from "@tanstack/react-query";

import { AZURE_REGIONS } from "@/constants";
import { isAksManagedResourceGroup } from "@/lib/aksManagedRg";

import { ErrorMsg } from "../ErrorMsg";
import { RgField } from "../RgField";
import type { ResourceConfig } from "../types";
import type { ValidationErrors } from "../validation";

interface RgRow {
  name: string;
  location: string;
  tags?: Record<string, string>;
}

function regionOptionsFor(region: string) {
  if (!region || AZURE_REGIONS.some((option) => option.value === region)) {
    return AZURE_REGIONS;
  }

  return [{ value: region, label: `${region} (current)` }, ...AZURE_REGIONS];
}

export function Step2ResourceGroups({
  config,
  setConfig,
  errors,
  rgQuery,
}: {
  config: ResourceConfig;
  setConfig: React.Dispatch<React.SetStateAction<ResourceConfig>>;
  errors: ValidationErrors;
  rgQuery: UseQueryResult<RgRow[]>;
}) {
  const regionOptions = regionOptionsFor(config.region);
  const selectableResourceGroups = rgQuery.data?.filter(
    (rg) => !isAksManagedResourceGroup(rg),
  );

  return (
    <div>
      <h2 style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>Project Folders</h2>
      <p
        style={{
          fontSize: 12,
          color: "var(--text-muted)",
          marginBottom: 14,
          lineHeight: 1.5,
        }}
      >
        Resource groups are like project folders that organize your Azure resources.
        You'll need one for BLAST workloads and one for the container registry. We can
        create them for you if they don't exist yet.
      </p>

      <div
        style={{
          marginBottom: 14,
          padding: "10px 12px",
          background: "rgba(110,159,255,0.06)",
          border: "1px solid rgba(110,159,255,0.15)",
          borderRadius: "var(--radius)",
        }}
      >
        <label
          className="glass-label"
          htmlFor="wizard-primary-region"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            marginBottom: 6,
          }}
        >
          <MapPin size={13} /> Primary Region
        </label>
        <select
          id="wizard-primary-region"
          className="glass-input"
          value={config.region}
          onChange={(event) =>
            setConfig((current) => ({ ...current, region: event.target.value }))
          }
          style={{ fontSize: 12 }}
        >
          {regionOptions.map((region) => (
            <option key={region.value} value={region.value}>
              {region.label}
            </option>
          ))}
        </select>
        <div
          style={{
            color: "var(--text-faint)",
            fontSize: 11,
            lineHeight: 1.5,
            marginTop: 6,
          }}
        >
          Used for new Storage and ACR resources. Selecting an existing workload resource
          group suggests its Azure location.
        </div>
        <ErrorMsg msg={errors.region} />
      </div>

      {rgQuery.isLoading ? (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            color: "var(--text-muted)",
          }}
        >
          <Loader2 size={14} className="spin" /> Loading resource groups...
        </div>
      ) : (
        <>
          {rgQuery.isError && (
            <div
              style={{
                color: "var(--warning)",
                fontSize: 12,
                lineHeight: 1.5,
                marginBottom: 10,
              }}
            >
              Could not list resource groups. Enter names manually:
            </div>
          )}

          {/* ── BLAST Workload ── */}
          <div className="wiz-section-header">
            <span className="wiz-section-icon">🔬</span>
            BLAST Workload
          </div>

          <RgField
            label="Workload Resource Group"
            configKey="workloadResourceGroup"
            placeholder="rg-elb"
            config={config}
            setConfig={setConfig}
            rgData={selectableResourceGroups}
            isManual={rgQuery.isError || !selectableResourceGroups?.length}
            error={errors.workloadResourceGroup}
            isPrimary
            tooltip={
              <>
                <strong>Workload Resource Group</strong>
                <br />
                Contains the resources for running BLAST searches.
                <div className="tt-resources">
                  <div className="tt-resource">
                    <span className="tt-icon">☸</span> <strong>AKS Cluster</strong> — runs
                    BLAST jobs on Kubernetes
                  </div>
                  <div className="tt-resource">
                    <span className="tt-icon">🗄</span> <strong>Storage Account</strong> —
                    holds blast-db, queries, and results
                  </div>
                </div>
                <div className="tt-note">
                  Tip: Create separate RGs for different projects (e.g. rg-elb-projectA,
                  rg-elb-projectB). Each gets its own AKS + Storage.
                </div>
              </>
            }
          />

          {/* ── Shared Infrastructure ── */}
          <div className="wiz-section-header">
            <span className="wiz-section-icon">🏗</span>
            Shared Infrastructure
            <span className="wiz-shared-badge">shared across all workloads</span>
          </div>

          <RgField
            label="ACR Resource Group"
            configKey="acrResourceGroup"
            placeholder="rg-elbacr"
            config={config}
            setConfig={setConfig}
            rgData={selectableResourceGroups}
            isManual={rgQuery.isError || !selectableResourceGroups?.length}
            error={errors.acrResourceGroup}
            tooltip={
              <>
                <strong>Container Registry (ACR)</strong>
                <br />
                Holds the pre-built Docker images needed by ElasticBLAST.
                <div className="tt-resources">
                  <div className="tt-resource">
                    <span className="tt-icon">📦</span> <strong>ncbi/elb</strong> —
                    ElasticBLAST runtime (1.4.0)
                  </div>
                  <div className="tt-resource">
                    <span className="tt-icon">📦</span>{" "}
                    <strong>ncbi/elb-job-submit</strong> — Job submission (4.1.0)
                  </div>
                  <div className="tt-resource">
                    <span className="tt-icon">📦</span>{" "}
                    <strong>ncbi/elb-query-split</strong> — Query splitter (0.1.4)
                  </div>
                </div>
                <div className="tt-note">
                  You only need one ACR. It is shared by all workload RGs. Images are
                  built once and reused.
                </div>
              </>
            }
          />

          <div
            style={{
              marginTop: 12,
              padding: "10px 12px",
              background: "rgba(110,159,255,0.06)",
              border: "1px solid rgba(110,159,255,0.15)",
              borderRadius: "var(--radius)",
              fontSize: 12,
              color: "var(--text-muted)",
              lineHeight: 1.5,
            }}
          >
            <strong style={{ color: "var(--text-primary)" }}>🗒 Terminal:</strong> The
            browser terminal runs as a sidecar inside this control plane — there is no
            Linux VM to provision. Open it from the dashboard “Terminal” card after setup.
          </div>
        </>
      )}
    </div>
  );
}
