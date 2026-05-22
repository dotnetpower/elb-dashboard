import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Clock, Database, Loader2, RefreshCw } from "lucide-react";
import { Link } from "react-router-dom";

import { dbVersionApi } from "@/api/endpoints";
import { loadSavedConfig } from "@/components/SetupWizard";
import { isFeatureEnabled } from "@/config/runtime";
import { SectionHeader, SetupRequired } from "@/pages/tools/ToolLayout";
import type { TabMeta } from "@/pages/tools/toolsPageModel";

export function DbVersionsTab({
  meta,
  hasConfig,
}: {
  meta: TabMeta;
  hasConfig: boolean;
}) {
  const cfg = loadSavedConfig();
  const customDbEnabled = isFeatureEnabled("customDb");
  const listQuery = useQuery({
    queryKey: ["db-versions", cfg?.storageAccountName],
    queryFn: () =>
      dbVersionApi.list(
        cfg?.subscriptionId ?? "",
        cfg?.storageAccountName ?? "",
        cfg?.workloadResourceGroup ?? "",
      ),
    enabled: !!cfg?.subscriptionId && !!cfg?.storageAccountName,
    staleTime: 30_000,
  });

  const versions = listQuery.data?.versions ?? [];

  if (!hasConfig) {
    return (
      <section className="glass-card blast-section">
        <SectionHeader
          icon={<Clock size={16} strokeWidth={1.5} />}
          title={meta.label}
          subtitle={meta.desc}
        />
        <SetupRequired feature="DB version registry" />
      </section>
    );
  }

  return (
    <section className="glass-card blast-section">
      <SectionHeader
        icon={<Clock size={16} strokeWidth={1.5} />}
        title={meta.label}
        subtitle={meta.desc}
        rightSlot={
          <button
            className="btn btn--ghost btn--sm"
            onClick={() => listQuery.refetch()}
            disabled={listQuery.isFetching}
            title="Refresh"
          >
            <RefreshCw size={12} className={listQuery.isFetching ? "spin" : ""} />
          </button>
        }
      />

      {listQuery.isLoading ? (
        <div className="empty-state">
          <div className="empty-state__icon">
            <Loader2 size={20} className="spin" />
          </div>
          <div className="empty-state__title">Loading database metadata…</div>
        </div>
      ) : versions.length === 0 ? (
        <div className="empty-state">
          <div className="empty-state__icon">
            <Database size={20} strokeWidth={1.5} />
          </div>
          <div className="empty-state__title">No database metadata yet</div>
          <div className="empty-state__desc">
            Build a custom database or download a public NCBI database to populate the
            registry.
          </div>
          {customDbEnabled && (
            <Link
              to="/blast/databases/build"
              className="btn btn--primary btn--sm"
              style={{ marginTop: 12 }}
            >
              Build custom database <ArrowRight size={12} />
            </Link>
          )}
        </div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table className="table" style={{ width: "100%", fontSize: 12 }}>
            <thead>
              <tr>
                <th>Database</th>
                <th>Type</th>
                <th>Source</th>
                <th>Version</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {versions.map((v, i) => (
                <tr key={i}>
                  <td>
                    <code className="code-val" style={{ fontWeight: 600 }}>
                      {v.db_name}
                    </code>
                  </td>
                  <td>{v.db_type ?? "—"}</td>
                  <td>
                    <span
                      className={`badge badge--${v.source === "ncbi" ? "info" : "muted"}`}
                    >
                      {v.source ?? "custom"}
                    </span>
                  </td>
                  <td>{v.source_version || v.version_tag || "—"}</td>
                  <td className="muted">
                    {v.created_at ? new Date(v.created_at).toLocaleDateString() : "—"}
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
