import { Link } from "react-router-dom";
import { memo } from "react";
import { Server, Trash2 } from "lucide-react";

import type { BlastJobSummary } from "@/api/endpoints";
import {
  isActiveJobState,
  toJobRowView,
} from "@/components/cards/ClusterBento/jobMapping";
import { statusColor } from "@/constants";

import { timeAgo } from "./dateGroup";

export interface JobRowProps {
  job: BlastJobSummary;
  onDelete: (id: string) => void;
  deleting: boolean;
  now?: number;
}

function formatDuration(seconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(seconds));
  const days = Math.floor(safeSeconds / 86_400);
  const hours = Math.floor((safeSeconds % 86_400) / 3_600);
  const minutes = Math.floor((safeSeconds % 3_600) / 60);
  const secs = safeSeconds % 60;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

function runtimeLabel(job: BlastJobSummary, active: boolean, now: number) {
  const created = Date.parse(job.created_at || "");
  if (!Number.isFinite(created)) return null;
  const finished = Date.parse(job.updated_at || "");
  const end = active ? now : finished;
  if (!Number.isFinite(end) || end < created) return null;
  const seconds = Math.floor((end - created) / 1000);
  return {
    label: active ? "Elapsed" : "Duration",
    value: formatDuration(seconds),
  };
}

function JobRowComponent({ job, onDelete, deleting, now = Date.now() }: JobRowProps) {
  const view = toJobRowView(job);
  const phase = view.state;
  const color = statusColor(phase.toLowerCase());
  const runtime = runtimeLabel(job, isActiveJobState(phase), now);
  const cluster = job.infrastructure?.cluster_name;
  const upn = job.owner_upn;
  const shortUser = upn ? upn.split("@")[0] : null;
  const splitChildren = job.split_children;
  const splitLabel = splitChildren ? `${splitChildren.child_count} child jobs` : null;

  return (
    <tr style={{ borderBottom: "1px solid var(--border-weak)" }}>
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
              title={view.title || job.job_id}
            >
              {view.title || job.job_id}
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
                {job.program} · {view.db}
              </span>
              {view.query && view.query !== view.title && <span>{view.query}</span>}
              {view.note && <span>{view.note}</span>}
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
              {splitLabel && (
                <span
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 2,
                    padding: "0 4px",
                    borderRadius: 3,
                    background: "var(--glass-bg-strong)",
                  }}
                  title={Object.entries(splitChildren?.children_by_status ?? {})
                    .map(([status, count]) => `${status}: ${count}`)
                    .join(", ")}
                >
                  {splitLabel}
                </span>
              )}
            </div>
          </div>
        </div>
      </td>
      <td
        style={{ padding: "8px 6px", fontSize: 11, whiteSpace: "nowrap" }}
        className="muted"
        title={upn || ""}
      >
        {shortUser || "—"}
      </td>
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
        <div>{job.created_at ? timeAgo(job.created_at) : "—"}</div>
        {runtime && (
          <div
            style={{
              fontSize: 10,
              marginTop: 1,
              color: "var(--text-faint)",
            }}
            title={`${runtime.label} ${runtime.value}`}
          >
            {runtime.label} {runtime.value}
          </div>
        )}
      </td>
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

export const JobRow = memo(JobRowComponent);
