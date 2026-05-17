import {
  AlertTriangle,
  ArrowRight,
  Check,
  CheckCircle2,
  Copy,
  Database,
  FlaskConical,
  Loader2,
} from "lucide-react";
import { Link } from "react-router-dom";

import { formatApiError } from "@/api/client";

import { SectionHeader } from "./SectionHeader";
import type { DatabaseBuilderState } from "./useDatabaseBuilderState";

export interface BuildSectionProps {
  dbName: DatabaseBuilderState["dbName"];
  dbType: DatabaseBuilderState["dbType"];
  fastaStats: DatabaseBuilderState["fastaStats"];
  readiness: DatabaseBuilderState["readiness"];
  readyCount: DatabaseBuilderState["readyCount"];
  allReady: DatabaseBuilderState["allReady"];
  buildMutation: DatabaseBuilderState["buildMutation"];
  successPath: DatabaseBuilderState["successPath"];
  copied: DatabaseBuilderState["copied"];
  handleCopyPath: DatabaseBuilderState["handleCopyPath"];
}

export function BuildSection({
  dbName,
  dbType,
  fastaStats,
  readiness,
  readyCount,
  allReady,
  buildMutation,
  successPath,
  copied,
  handleCopyPath,
}: BuildSectionProps) {
  return (
    <section className="glass-card blast-section">
      <SectionHeader
        step={3}
        icon={<FlaskConical size={16} strokeWidth={1.5} />}
        title="Build database"
        subtitle="Runs makeblastdb in the terminal sidecar, then publishes to blob storage"
      />

      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 16,
          flexWrap: "wrap",
        }}
      >
        <div className="muted" style={{ fontSize: 12, maxWidth: 520 }}>
          {allReady
            ? `Ready to build "${dbName}" (${dbType === "nucl" ? "nucleotide" : "protein"}) with ${fastaStats.seqCount} sequence${fastaStats.seqCount !== 1 ? "s" : ""}.`
            : `Complete ${readiness.length - readyCount} more step${readiness.length - readyCount !== 1 ? "s" : ""} to enable building.`}
        </div>
        <button
          type="button"
          className="blast-submit-btn"
          disabled={!allReady}
          onClick={() => buildMutation.mutate()}
        >
          {buildMutation.isPending ? (
            <>
              <Loader2 size={16} className="spin" />
              Building database…
            </>
          ) : (
            <>
              <Database size={16} />
              Build database
            </>
          )}
        </button>
      </div>

      {buildMutation.isPending && (
        <div
          className="muted"
          style={{
            marginTop: 12,
            fontSize: 12,
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          <Loader2 size={12} className="spin" />
          Building may take a few minutes depending on input size — keep this tab open.
        </div>
      )}

      {buildMutation.isSuccess && buildMutation.data && (
        <div
          style={{
            marginTop: 16,
            padding: 16,
            borderRadius: 12,
            background: "rgba(106,214,163,0.06)",
            border: "1px solid rgba(106,214,163,0.25)",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              marginBottom: 10,
              color: "var(--success)",
              fontWeight: 600,
            }}
          >
            <CheckCircle2 size={16} /> Database created
          </div>
          <div className="metric-grid" style={{ marginTop: 0 }}>
            <div className="metric-block">
              <div className="mv">{buildMutation.data.db_name}</div>
              <div className="mu">Name</div>
            </div>
            <div className="metric-block">
              <div className="mv">
                {buildMutation.data.db_type === "prot" ? "Protein" : "Nucleotide"}
              </div>
              <div className="mu">Type</div>
            </div>
            <div className="metric-block">
              <div className="mv">{buildMutation.data.file_count}</div>
              <div className="mu">Files</div>
            </div>
          </div>
          <div
            style={{
              marginTop: 12,
              display: "flex",
              alignItems: "center",
              gap: 8,
              flexWrap: "wrap",
            }}
          >
            <span className="muted" style={{ fontSize: 12 }}>
              Use this path when submitting a job:
            </span>
            <code className="code-val">{successPath}</code>
            <button
              type="button"
              className={`copy-btn${copied ? " copy-btn--copied" : ""}`}
              onClick={handleCopyPath}
              aria-label="Copy database path"
            >
              {copied ? <Check size={12} /> : <Copy size={12} />}{" "}
              {copied ? "Copied" : "Copy"}
            </button>
            <Link
              to="/blast/submit"
              className="btn btn--primary btn--sm"
              style={{ marginLeft: "auto" }}
            >
              Run a search <ArrowRight size={12} />
            </Link>
          </div>
        </div>
      )}

      {buildMutation.isError && (
        <div
          style={{
            marginTop: 16,
            padding: 12,
            borderRadius: 10,
            background: "rgba(224,123,138,0.08)",
            border: "1px solid rgba(224,123,138,0.25)",
            color: "var(--danger)",
            fontSize: 12,
          }}
        >
          <AlertTriangle size={13} style={{ verticalAlign: "-2px", marginRight: 6 }} />
          {formatApiError(buildMutation.error, "blast")}
        </div>
      )}
    </section>
  );
}
