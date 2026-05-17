import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Loader2, RefreshCw, Shield } from "lucide-react";

import { auditApi } from "@/api/endpoints";
import { SectionHeader, StatBox } from "@/pages/tools/ToolLayout";
import type { TabMeta } from "@/pages/tools/toolsPageModel";

export function AuditTrailTab({ meta }: { meta: TabMeta }) {
  const [actionFilter, setActionFilter] = useState("");

  const listQuery = useQuery({
    queryKey: ["audit-trail", actionFilter],
    queryFn: () => auditApi.listEvents(200, actionFilter || undefined),
    staleTime: 15_000,
  });

  const events = useMemo(() => listQuery.data?.events ?? [], [listQuery.data?.events]);
  const summary = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const ev of events) {
      const action = ev.action ?? "unknown";
      counts[action] = (counts[action] ?? 0) + 1;
    }
    return counts;
  }, [events]);

  return (
    <section className="glass-card blast-section">
      <SectionHeader
        icon={<Shield size={16} strokeWidth={1.5} />}
        title={meta.label}
        subtitle={meta.desc}
        rightSlot={
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <select
              className="form-input form-input--compact"
              value={actionFilter}
              onChange={(e) => setActionFilter(e.target.value)}
            >
              <option value="">All actions</option>
              <option value="blast_submit">BLAST Submit</option>
              <option value="blast_delete">BLAST Delete</option>
              <option value="db_build">DB Build</option>
              <option value="terminal_provision">Terminal Provision</option>
            </select>
            <button
              className="btn btn--ghost btn--sm"
              onClick={() => listQuery.refetch()}
              disabled={listQuery.isFetching}
              title="Refresh"
            >
              <RefreshCw size={12} className={listQuery.isFetching ? "spin" : ""} />
            </button>
          </div>
        }
      />

      {events.length > 0 && (
        <div className="metric-grid" style={{ marginBottom: 16 }}>
          <StatBox label="Events" value={events.length} accent />
          {Object.entries(summary)
            .sort((a, b) => b[1] - a[1])
            .slice(0, 3)
            .map(([action, count]) => (
              <StatBox key={action} label={action} value={count} />
            ))}
        </div>
      )}

      {listQuery.isLoading ? (
        <div className="empty-state">
          <div className="empty-state__icon">
            <Loader2 size={20} className="spin" />
          </div>
          <div className="empty-state__title">Loading audit events…</div>
        </div>
      ) : events.length === 0 ? (
        <div className="empty-state">
          <div className="empty-state__icon">
            <Shield size={20} strokeWidth={1.5} />
          </div>
          <div className="empty-state__title">No audit events yet</div>
          <div className="empty-state__desc">
            Operations like BLAST submissions, database builds, and terminal provisioning
            will appear here automatically.
          </div>
        </div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table className="table" style={{ width: "100%", fontSize: 12 }}>
            <thead>
              <tr>
                <th>Time</th>
                <th>Action</th>
                <th>User</th>
                <th>Job ID</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {events.map((ev, i) => (
                <tr key={i}>
                  <td className="muted" style={{ whiteSpace: "nowrap" }}>
                    {ev.timestamp ? new Date(ev.timestamp).toLocaleString() : "—"}
                  </td>
                  <td>
                    <span className="badge badge--info">{ev.action}</span>
                  </td>
                  <td>{ev.user ?? "—"}</td>
                  <td>
                    <code className="code-val">{ev.job_id ?? "—"}</code>
                  </td>
                  <td
                    className="muted"
                    style={{
                      maxWidth: 320,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {ev.details ? JSON.stringify(ev.details).slice(0, 100) : "—"}
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
