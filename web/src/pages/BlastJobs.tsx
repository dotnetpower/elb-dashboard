import { useState, useMemo, useCallback } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Trash2,
  RefreshCw,
  AlertTriangle,
  Search,
  ChevronDown,
  Server,
} from "lucide-react";

import { blastApi, type BlastJobSummary } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import { statusColor } from "@/constants";
import { ConfirmDialog } from "@/components/ConfirmDialog";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ${mins % 60}m ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

type DateGroup = "Today" | "Yesterday" | "This Week" | "Earlier";

function getDateGroup(dateStr: string): DateGroup {
  const d = new Date(dateStr);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today.getTime() - 86400_000);
  const weekAgo = new Date(today.getTime() - 6 * 86400_000);
  if (d >= today) return "Today";
  if (d >= yesterday) return "Yesterday";
  if (d >= weekAgo) return "This Week";
  return "Earlier";
}

const GROUP_ORDER: DateGroup[] = ["Today", "Yesterday", "This Week", "Earlier"];

const FAILED_PHASES = ["failed", "submit_failed", "error"];
const TERMINAL_PHASES = ["completed", ...FAILED_PHASES, "deleted", "cancelled"];

// ---------------------------------------------------------------------------
// JobRow — compact table row
// ---------------------------------------------------------------------------
function JobRow({
  job,
  onDelete,
  deleting,
}: {
  job: BlastJobSummary;
  onDelete: (id: string) => void;
  deleting: boolean;
}) {
  const phase = job.phase || job.status;
  const color = statusColor(phase);
  const cluster = job.infrastructure?.cluster_name;
  const upn = job.owner_upn;
  const shortUser = upn ? upn.split("@")[0] : null;

  return (
    <tr style={{ borderBottom: "1px solid var(--border-weak)" }}>
      {/* Status dot + Title + subtitle */}
      <td style={{ padding: "8px 0" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span
            style={{
              width: 7,
              height: 7,
              borderRadius: 999,
              background: color,
              flexShrink: 0,
            }}
          />
          <div style={{ minWidth: 0 }}>
            <Link
              to={`/blast/jobs/${encodeURIComponent(job.job_id)}`}
              style={{
                textDecoration: "none",
                color: "inherit",
                fontWeight: 600,
                fontSize: 13,
                display: "block",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
              title={job.job_title || job.job_id}
            >
              {job.job_title || job.job_id}
            </Link>
            <div
              className="muted"
              style={{
                fontSize: 10,
                marginTop: 1,
                display: "flex",
                alignItems: "center",
                gap: 6,
                flexWrap: "wrap",
              }}
            >
              <span>
                {job.program} · {(job.db ?? "").split("/").pop()}
              </span>
              {cluster && (
                <span
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 2,
                    padding: "0 4px",
                    borderRadius: 3,
                    background: "var(--glass-bg-strong)",
                  }}
                >
                  <Server size={8} strokeWidth={1.5} /> {cluster}
                </span>
              )}
            </div>
          </div>
        </div>
      </td>
      {/* User */}
      <td
        style={{ padding: "8px 6px", fontSize: 11, whiteSpace: "nowrap" }}
        className="muted"
        title={upn || ""}
      >
        {shortUser || "—"}
      </td>
      {/* Status badge */}
      <td style={{ padding: "8px 6px", textAlign: "center" }}>
        <span
          style={{
            fontSize: 10,
            textTransform: "uppercase",
            letterSpacing: "0.04em",
            padding: "2px 6px",
            borderRadius: 4,
            background: `${color}18`,
            color,
            fontWeight: 600,
            whiteSpace: "nowrap",
          }}
        >
          {phase}
        </span>
      </td>
      {/* Time */}
      <td
        style={{
          padding: "8px 6px",
          fontSize: 11,
          whiteSpace: "nowrap",
          textAlign: "right",
        }}
        className="muted"
        title={job.created_at ? new Date(job.created_at).toLocaleString() : ""}
      >
        {job.created_at ? timeAgo(job.created_at) : "—"}
      </td>
      {/* Actions */}
      <td style={{ padding: "8px 0", textAlign: "right", width: 36 }}>
        <button
          className="glass-button"
          onClick={() => onDelete(job.job_id)}
          disabled={deleting}
          style={{ padding: "3px 5px" }}
          title="Delete"
        >
          <Trash2 size={12} strokeWidth={1.5} />
        </button>
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// DateGroupSection — collapsible section with date header
// ---------------------------------------------------------------------------
function DateGroupSection({
  label,
  jobs,
  defaultOpen,
  onDelete,
  deleting,
}: {
  label: DateGroup;
  jobs: BlastJobSummary[];
  defaultOpen: boolean;
  onDelete: (id: string) => void;
  deleting: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const runningCount = jobs.filter(
    (j) => !TERMINAL_PHASES.includes(j.phase || j.status),
  ).length;

  return (
    <div>
      <button
        onClick={() => setOpen((o) => !o)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          width: "100%",
          background: "none",
          border: "none",
          cursor: "pointer",
          padding: "6px 0",
          color: "var(--text-primary)",
        }}
      >
        <ChevronDown
          size={13}
          style={{
            transform: open ? "rotate(0deg)" : "rotate(-90deg)",
            transition: "transform 0.15s ease",
            color: "var(--text-faint)",
          }}
        />
        <span style={{ fontSize: 12, fontWeight: 600 }}>{label}</span>
        <span className="muted" style={{ fontSize: 11 }}>
          {jobs.length} job{jobs.length !== 1 ? "s" : ""}
          {runningCount > 0 && (
            <span style={{ color: "var(--warning)", marginLeft: 6 }}>
              {runningCount} active
            </span>
          )}
        </span>
      </button>
      {open && (
        <div className="table-scroll" style={{ marginBottom: "var(--space-3)" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border-weak)" }}>
                <th
                  style={{
                    textAlign: "left",
                    padding: "4px 0",
                    color: "var(--text-faint)",
                    fontSize: 10,
                    textTransform: "uppercase",
                    fontWeight: 500,
                  }}
                >
                  Job
                </th>
                <th
                  style={{
                    textAlign: "left",
                    padding: "4px 6px",
                    color: "var(--text-faint)",
                    fontSize: 10,
                    textTransform: "uppercase",
                    fontWeight: 500,
                  }}
                >
                  User
                </th>
                <th
                  style={{
                    textAlign: "center",
                    padding: "4px 6px",
                    color: "var(--text-faint)",
                    fontSize: 10,
                    textTransform: "uppercase",
                    fontWeight: 500,
                  }}
                >
                  Status
                </th>
                <th
                  style={{
                    textAlign: "right",
                    padding: "4px 6px",
                    color: "var(--text-faint)",
                    fontSize: 10,
                    textTransform: "uppercase",
                    fontWeight: 500,
                  }}
                >
                  Time
                </th>
                <th style={{ width: 36 }} />
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <JobRow
                  key={job.job_id}
                  job={job}
                  onDelete={onDelete}
                  deleting={deleting}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------
export function BlastJobs() {
  const queryClient = useQueryClient();
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "running" | "completed" | "failed">("all");
  const [search, setSearch] = useState("");

  const jobsQuery = useQuery({
    queryKey: ["blast-jobs"],
    queryFn: () => blastApi.listJobs(),
    refetchInterval: 20_000,
  });

  const deleteMutation = useMutation({
    mutationFn: (jobId: string) => blastApi.deleteJob(jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["blast-jobs"] });
    },
  });

  const allJobs = useMemo(() => jobsQuery.data?.jobs ?? [], [jobsQuery.data?.jobs]);

  // Filter + search
  const filtered = useMemo(() => {
    let list = [...allJobs].reverse();
    if (filter !== "all") {
      list = list.filter((j) => {
        const phase = j.phase || j.status;
        if (filter === "running") return !TERMINAL_PHASES.includes(phase);
        if (filter === "failed") return FAILED_PHASES.includes(phase);
        return phase === filter;
      });
    }
    if (search.trim()) {
      const q = search.toLowerCase();
      list = list.filter(
        (j) =>
          (j.job_title ?? "").toLowerCase().includes(q) ||
          j.job_id.toLowerCase().includes(q) ||
          (j.program ?? "").toLowerCase().includes(q) ||
          (j.db ?? "").toLowerCase().includes(q) ||
          (j.infrastructure?.cluster_name ?? "").toLowerCase().includes(q),
      );
    }
    return list;
  }, [allJobs, filter, search]);

  // Group by date
  const grouped = useMemo(() => {
    const map = new Map<DateGroup, BlastJobSummary[]>();
    for (const g of GROUP_ORDER) map.set(g, []);
    for (const job of filtered) {
      const group = job.created_at ? getDateGroup(job.created_at) : "Earlier";
      map.get(group)!.push(job);
    }
    return GROUP_ORDER.filter((g) => (map.get(g)?.length ?? 0) > 0).map((g) => ({
      label: g,
      jobs: map.get(g)!,
    }));
  }, [filtered]);

  const counts = useMemo(() => {
    const c = { running: 0, completed: 0, failed: 0 };
    allJobs.forEach((j) => {
      const p = j.phase || j.status;
      if (p === "completed") c.completed++;
      else if (FAILED_PHASES.includes(p)) c.failed++;
      else if (p !== "deleted") c.running++;
    });
    return c;
  }, [allJobs]);

  const handleDelete = useCallback((id: string) => setDeleteTarget(id), []);

  return (
    <div className="page-stack">
      <header
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          gap: 16,
          flexWrap: "wrap",
        }}
      >
        <div className="page-header" style={{ marginBottom: 0 }}>
          <div className="page-header__title">BLAST Jobs</div>
          <div className="page-header__desc">
            {allJobs.length} total · {counts.running} running · {counts.completed}{" "}
            completed · {counts.failed} failed
          </div>
          {allJobs.length > 0 && (
            <div className="prog" style={{ width: 200, height: 6, marginTop: 6 }}>
              <div style={{ display: "flex", height: "100%" }}>
                <div
                  style={{
                    width: `${(counts.completed / allJobs.length) * 100}%`,
                    background: "var(--success)",
                    borderRadius: "2px 0 0 2px",
                  }}
                />
                <div
                  style={{
                    width: `${(counts.running / allJobs.length) * 100}%`,
                    background: "var(--warning)",
                  }}
                />
                <div
                  style={{
                    width: `${(counts.failed / allJobs.length) * 100}%`,
                    background: "var(--danger)",
                    borderRadius: "0 2px 2px 0",
                  }}
                />
              </div>
            </div>
          )}
        </div>
        <div style={{ display: "flex", gap: "var(--space-3)" }}>
          <Link
            to="/blast/submit"
            className="glass-button glass-button--primary"
            style={{ textDecoration: "none" }}
          >
            New search
          </Link>
          <button
            className="glass-button"
            onClick={() => jobsQuery.refetch()}
            disabled={jobsQuery.isFetching}
          >
            <RefreshCw size={14} strokeWidth={1.5} /> Refresh
          </button>
        </div>
      </header>

      {/* Loading skeleton */}
      {jobsQuery.isLoading && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {[1, 2, 3].map((i) => (
            <div
              key={i}
              className="skeleton skeleton-line"
              style={{ height: 40, width: "100%" }}
            />
          ))}
        </div>
      )}

      {/* Filter bar + Search */}
      {allJobs.length > 0 && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "var(--space-3)",
            flexWrap: "wrap",
          }}
        >
          <div style={{ display: "flex", gap: "var(--space-2)" }}>
            {(["all", "running", "completed", "failed"] as const).map((f) => (
              <button
                key={f}
                className={`glass-button ${filter === f ? "glass-button--primary" : ""}`}
                onClick={() => setFilter(f)}
                style={{ fontSize: 11, textTransform: "capitalize" }}
              >
                {f} {f !== "all" && `(${counts[f]})`}
              </button>
            ))}
          </div>
          <div style={{ position: "relative", flex: "1 1 180px", maxWidth: 280 }}>
            <Search
              size={13}
              style={{
                position: "absolute",
                left: 8,
                top: "50%",
                transform: "translateY(-50%)",
                color: "var(--text-faint)",
                pointerEvents: "none",
              }}
            />
            <input
              type="text"
              placeholder="Search jobs…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{
                width: "100%",
                padding: "5px 8px 5px 26px",
                background: "var(--glass-bg)",
                border: "1px solid var(--border-weak)",
                borderRadius: 6,
                color: "var(--text-primary)",
                fontSize: 12,
                outline: "none",
              }}
            />
          </div>
        </div>
      )}

      {/* Delete error */}
      {deleteMutation.isError && (
        <div
          style={{
            padding: "8px 12px",
            background: "rgba(224,123,138,0.08)",
            border: "1px solid rgba(224,123,138,0.2)",
            borderRadius: 6,
            fontSize: 12,
            color: "var(--danger)",
          }}
        >
          <AlertTriangle size={12} style={{ verticalAlign: "middle", marginRight: 4 }} />
          Delete failed: {formatApiError(deleteMutation.error, "blast")}
        </div>
      )}

      {/* Empty states */}
      {allJobs.length === 0 && !jobsQuery.isLoading && (
        <section
          className="glass-card"
          style={{ textAlign: "center", padding: "var(--space-7)" }}
        >
          <p className="muted">No BLAST jobs yet.</p>
          <Link
            to="/blast/submit"
            className="glass-button glass-button--primary"
            style={{ marginTop: "var(--space-4)", textDecoration: "none" }}
          >
            Submit your first search
          </Link>
        </section>
      )}
      {filtered.length === 0 && allJobs.length > 0 && !jobsQuery.isLoading && (
        <section
          className="glass-card"
          style={{ textAlign: "center", padding: "var(--space-5)" }}
        >
          <p className="muted">
            {search ? `No jobs matching "${search}"` : `No ${filter} jobs.`}
          </p>
        </section>
      )}

      {/* Date-grouped job tables */}
      {grouped.map(({ label, jobs: groupJobs }) => (
        <DateGroupSection
          key={label}
          label={label}
          jobs={groupJobs}
          defaultOpen={label !== "Earlier"}
          onDelete={handleDelete}
          deleting={deleteMutation.isPending}
        />
      ))}

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete BLAST Job"
        message="This will stop the job and clean up resources. This cannot be undone."
        confirmLabel="Delete"
        onConfirm={() => {
          if (deleteTarget) deleteMutation.mutate(deleteTarget);
          setDeleteTarget(null);
        }}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
