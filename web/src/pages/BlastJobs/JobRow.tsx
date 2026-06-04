import { Link } from "react-router-dom";
import { memo } from "react";
import { Server, Trash2 } from "lucide-react";

import type { BlastJobSummary } from "@/api/endpoints";
import {
  isActiveJobState,
  isQueuedJobState,
  toJobRowView,
} from "@/components/cards/ClusterBento/jobMapping";
import type { DisplayJobState } from "@/components/cards/ClusterBento/jobTypes";
import { queueReasonText, statusColor } from "@/constants";

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

function runtimeLabel(job: BlastJobSummary, state: DisplayJobState, now: number) {
  const created = Date.parse(job.created_at || "");
  if (!Number.isFinite(created)) return null;
  const active = isActiveJobState(state);
  const finished = Date.parse(job.updated_at || "");
  const end = active ? now : finished;
  if (!Number.isFinite(end) || end < created) return null;
  const seconds = Math.floor((end - created) / 1000);
  // A queued job is "active" but has not started running on the cluster, so
  // "Elapsed" reads wrong. Show how long it has been waiting in line instead.
  const label = isQueuedJobState(state) ? "Queued for" : active ? "Elapsed" : "Duration";
  return {
    label,
    value: formatDuration(seconds),
  };
}

function JobRowComponent({ job, onDelete, deleting, now = Date.now() }: JobRowProps) {
  const view = toJobRowView(job);
  const phase = view.state;
  const color = statusColor(phase.toLowerCase());
  const runtime = runtimeLabel(job, phase, now);
  // Why a queued job is waiting (submit slot / cluster capacity / queue), shown
  // as a calm secondary line under the QUEUED badge. Null for non-queued states.
  const queueReason = isQueuedJobState(phase) ? queueReasonText(job.phase) : null;
  const cluster = job.infrastructure?.cluster_name;
  const upn = job.owner_upn;
  // Legacy rows pre-dating owner_upn capture still carry submission_source on
  // the payload. Surfacing "api" for external_api submits keeps the User
  // column meaningful even before the table row is rewritten.
  const submissionSource =
    typeof job.payload?.submission_source === "string"
      ? (job.payload.submission_source as string)
      : null;
  const isApiSubmit = upn === "api" || submissionSource === "external_api";
  const shortUser = isApiSubmit ? "api" : upn ? upn.split("@")[0] : null;
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
        {queueReason && (
          <div
            style={{
              fontSize: 9,
              marginTop: 3,
              color: "var(--text-faint)",
              whiteSpace: "nowrap",
            }}
            title={queueReason}
          >
            {queueReason}
          </div>
        )}
        {job.stale && (
          <div
            style={{
              fontSize: 9,
              textTransform: "uppercase",
              letterSpacing: "0.04em",
              marginTop: 3,
              color: "var(--text-faint)",
              whiteSpace: "nowrap",
            }}
            title={
              job.refresh_blocked_reason === "cluster_not_found"
                ? "Cluster not found — last-known status, no longer refreshing."
                : `Cluster ${job.cluster_power_state || "stopped"} — status frozen until it restarts.`
            }
          >
            {job.refresh_blocked_reason === "cluster_not_found"
              ? "✕ no cluster"
              : "❄ frozen"}
          </div>
        )}
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
