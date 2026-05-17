import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  Calendar,
  Loader2,
  Play,
  RefreshCw,
  ToggleLeft,
  ToggleRight,
  Trash2,
} from "lucide-react";
import { Link } from "react-router-dom";

import { formatApiError } from "@/api/client";
import { scheduleApi } from "@/api/endpoints";
import { useToast } from "@/components/Toast";
import { SectionHeader } from "@/pages/tools/ToolLayout";
import type { TabMeta } from "@/pages/tools/toolsPageModel";

export function SchedulesTab({ meta }: { meta: TabMeta }) {
  const { toast } = useToast();
  const listQuery = useQuery({
    queryKey: ["blast-schedules"],
    queryFn: () => scheduleApi.list(),
    staleTime: 10_000,
  });

  const runMutation = useMutation({
    mutationFn: (id: string) => scheduleApi.run(id),
    onSuccess: (data) => toast(`Job started: ${data.job_id}`, "success"),
    onError: (err: unknown) => toast(formatApiError(err, "blast"), "error"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => scheduleApi.remove(id),
    onSuccess: () => {
      toast("Schedule deleted", "info");
      listQuery.refetch();
    },
  });

  const schedules = listQuery.data?.schedules ?? [];

  return (
    <section className="glass-card blast-section">
      <SectionHeader
        icon={<Calendar size={16} strokeWidth={1.5} />}
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
          <div className="empty-state__title">Loading schedules…</div>
        </div>
      ) : schedules.length === 0 ? (
        <div className="empty-state">
          <div className="empty-state__icon">
            <Calendar size={20} strokeWidth={1.5} />
          </div>
          <div className="empty-state__title">No schedules yet</div>
          <div className="empty-state__desc">
            Save a search from the New Search page to add it here for one-click or
            scheduled re-runs.
          </div>
          <Link
            to="/blast/submit"
            className="btn btn--primary btn--sm"
            style={{ marginTop: 12 }}
          >
            New BLAST search <ArrowRight size={12} />
          </Link>
        </div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table className="table" style={{ width: "100%", fontSize: 13 }}>
            <thead>
              <tr>
                <th>Name</th>
                <th>Trigger</th>
                <th>Runs</th>
                <th>Last run</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {schedules.map((s) => (
                <tr key={s.schedule_id}>
                  <td style={{ fontWeight: 600 }}>{s.name}</td>
                  <td>
                    <span className="badge badge--info">{s.trigger_type}</span>
                  </td>
                  <td>{s.run_count}</td>
                  <td className="muted">
                    {s.last_run ? new Date(s.last_run).toLocaleString() : "Never"}
                  </td>
                  <td>
                    {s.enabled ? (
                      <span
                        style={{
                          color: "var(--success)",
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 4,
                        }}
                      >
                        <ToggleRight size={14} /> Active
                      </span>
                    ) : (
                      <span
                        className="muted"
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 4,
                        }}
                      >
                        <ToggleLeft size={14} /> Paused
                      </span>
                    )}
                  </td>
                  <td style={{ display: "flex", gap: 4 }}>
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={() => runMutation.mutate(s.schedule_id)}
                      disabled={runMutation.isPending}
                      title="Run now"
                    >
                      <Play size={12} />
                    </button>
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={() => deleteMutation.mutate(s.schedule_id)}
                      style={{ color: "var(--danger)" }}
                      title="Delete"
                    >
                      <Trash2 size={12} />
                    </button>
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
