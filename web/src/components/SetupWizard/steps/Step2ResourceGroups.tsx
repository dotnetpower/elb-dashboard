import { Loader2 } from "lucide-react";
import type { UseQueryResult } from "@tanstack/react-query";

import { RgField } from "../RgField";
import type { ResourceConfig } from "../types";
import type { ValidationErrors } from "../validation";

interface RgRow {
  name: string;
  location: string;
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
  return (
    <div>
      <h2 style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>
        Project Folders
      </h2>
      <p
        style={{
          fontSize: 12,
          color: "var(--text-muted)",
          marginBottom: 14,
          lineHeight: 1.5,
        }}
      >
        Resource groups are like project folders that organize your Azure
        resources. You'll need one for BLAST workloads and one for the container
        registry. We can create them for you if they don't exist yet.
      </p>

      {config.region && (
        <div
          style={{
            marginBottom: 14,
            padding: "8px 12px",
            background: "rgba(110,159,255,0.06)",
            border: "1px solid rgba(110,159,255,0.15)",
            borderRadius: "var(--radius)",
            fontSize: 12,
            color: "var(--text-muted)",
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          <span style={{ fontSize: 14 }}>📍</span>
          Primary region (from Workload RG):{" "}
          <strong style={{ color: "var(--text-primary)", marginLeft: 4 }}>
            {config.region}
          </strong>
        </div>
      )}

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
            rgData={rgQuery.data}
            isManual={rgQuery.isError || !rgQuery.data?.length}
            error={errors.workloadResourceGroup}
            isPrimary
            tooltip={
              <>
                <strong>Workload Resource Group</strong>
                <br />
                Contains the resources for running BLAST searches.
                <div className="tt-resources">
                  <div className="tt-resource">
                    <span className="tt-icon">☸</span>{" "}
                    <strong>AKS Cluster</strong> — runs BLAST jobs on Kubernetes
                  </div>
                  <div className="tt-resource">
                    <span className="tt-icon">🗄</span>{" "}
                    <strong>Storage Account</strong> — holds blast-db, queries,
                    and results
                  </div>
                </div>
                <div className="tt-note">
                  Tip: Create separate RGs for different projects (e.g.
                  rg-elb-projectA, rg-elb-projectB). Each gets its own AKS +
                  Storage.
                </div>
              </>
            }
          />

          {/* ── Shared Infrastructure ── */}
          <div className="wiz-section-header">
            <span className="wiz-section-icon">🏗</span>
            Shared Infrastructure
            <span className="wiz-shared-badge">
              shared across all workloads
            </span>
          </div>

          <RgField
            label="ACR Resource Group"
            configKey="acrResourceGroup"
            placeholder="rg-elbacr"
            config={config}
            setConfig={setConfig}
            rgData={rgQuery.data}
            isManual={rgQuery.isError || !rgQuery.data?.length}
            error={errors.acrResourceGroup}
            tooltip={
              <>
                <strong>Container Registry (ACR)</strong>
                <br />
                Holds the pre-built Docker images needed by ElasticBLAST.
                <div className="tt-resources">
                  <div className="tt-resource">
                    <span className="tt-icon">📦</span>{" "}
                    <strong>ncbi/elb</strong> — ElasticBLAST runtime (1.4.0)
                  </div>
                  <div className="tt-resource">
                    <span className="tt-icon">📦</span>{" "}
                    <strong>ncbi/elb-job-submit</strong> — Job submission (4.1.0)
                  </div>
                  <div className="tt-resource">
                    <span className="tt-icon">📦</span>{" "}
                    <strong>ncbi/elb-query-split</strong> — Query splitter
                    (0.1.4)
                  </div>
                </div>
                <div className="tt-note">
                  You only need one ACR. It is shared by all workload RGs. Images
                  are built once and reused.
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
            <strong style={{ color: "var(--text-primary)" }}>🗒 Terminal:</strong>{" "}
            The browser terminal runs as a sidecar inside this control plane —
            there is no Linux VM to provision. Open it from the dashboard
            “Terminal” card after setup.
          </div>
        </>
      )}
    </div>
  );
}
