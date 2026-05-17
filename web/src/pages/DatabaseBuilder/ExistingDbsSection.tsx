import { Database, HardDrive, Loader2, RefreshCw } from "lucide-react";

import { SectionHeader } from "./SectionHeader";
import { formatBytes } from "./formatBytes";
import type { DatabaseBuilderState } from "./useDatabaseBuilderState";

export interface ExistingDbsSectionProps {
  cfg: DatabaseBuilderState["cfg"];
  dbListQuery: DatabaseBuilderState["dbListQuery"];
  existingDbs: DatabaseBuilderState["existingDbs"];
}

export function ExistingDbsSection({
  cfg,
  dbListQuery,
  existingDbs,
}: ExistingDbsSectionProps) {
  return (
    <section className="glass-card blast-section">
      <SectionHeader
        step={4}
        icon={<HardDrive size={16} strokeWidth={1.5} />}
        title="Existing databases"
        subtitle="All BLAST databases discovered in the configured storage account"
        rightSlot={
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => dbListQuery.refetch()}
            disabled={dbListQuery.isFetching}
            title="Refresh list"
          >
            <RefreshCw size={12} className={dbListQuery.isFetching ? "spin" : ""} />
          </button>
        }
      />

      {dbListQuery.isLoading ? (
        <div className="empty-state">
          <div className="empty-state__icon">
            <Loader2 size={20} className="spin" />
          </div>
          <div className="empty-state__title">Loading databases…</div>
        </div>
      ) : !cfg?.subscriptionId ? (
        <div className="empty-state">
          <div className="empty-state__icon">
            <HardDrive size={20} strokeWidth={1.5} />
          </div>
          <div className="empty-state__title">Workspace not configured</div>
          <div className="empty-state__desc">
            Configure a subscription and storage account to see your databases.
          </div>
        </div>
      ) : existingDbs.length === 0 ? (
        <div className="empty-state">
          <div className="empty-state__icon">
            <Database size={20} strokeWidth={1.5} />
          </div>
          <div className="empty-state__title">No databases yet</div>
          <div className="empty-state__desc">
            Build your first custom database above, or download a public NCBI database
            from the Storage card on the Dashboard.
          </div>
        </div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table className="table" style={{ width: "100%", fontSize: 13 }}>
            <thead>
              <tr>
                <th>Name</th>
                <th>Files</th>
                <th>Size</th>
                <th>Last modified</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {existingDbs.map((db) => (
                <tr key={db.name}>
                  <td>
                    <code className="code-val">{db.name}</code>
                  </td>
                  <td>{db.file_count ?? "—"}</td>
                  <td>{db.total_bytes ? formatBytes(db.total_bytes) : "—"}</td>
                  <td className="muted">
                    {db.last_modified
                      ? new Date(db.last_modified).toLocaleDateString()
                      : "—"}
                  </td>
                  <td>
                    <span
                      className={`badge badge--${db.source === "custom" ? "warning" : db.source_version ? "info" : "muted"}`}
                    >
                      {db.source === "custom"
                        ? "Custom"
                        : db.source_version
                          ? `NCBI ${db.source_version}`
                          : "NCBI"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
