import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  CheckCircle2,
  Loader2,
  AlertTriangle,
  Clock,
  ArrowRight,
  Search,
  type LucideIcon,
} from "lucide-react";

import { blastApi, type BlastJobSummary } from "@/api/endpoints";
import "./LatestJobChip.css";

/**
 * Topbar chip that surfaces the most recent BLAST job at a glance.
 *
 * Researcher's first question every morning is "did my last search
 * finish?". This chip answers that without making them open the Jobs
 * page first. Clicking opens the job detail (results when finished,
 * progress otherwise).
 *
 * When the tenant has no jobs yet, the chip becomes a one-click
 * shortcut to start a new search instead of disappearing — keeps the
 * topbar's affordance consistent across cold-start and steady-state.
 *
 * UI-only for now — uses the existing `/blast/jobs` endpoint and the
 * existing `BlastJobSummary` shape; no backend changes.
 */
export function LatestJobChip() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["latest-blast-job"],
    queryFn: () => blastApi.listJobs(),
    // Researcher leaves the dashboard open on a second monitor — keep
    // it warm but cheap. 15 s matches the existing dashboard cadence.
    refetchInterval: 15_000,
    staleTime: 10_000,
  });

  if (isLoading || isError) return null;

  const jobs = data?.jobs ?? [];
  if (jobs.length === 0) {
    return (
      <Link
        to="/blast/submit"
        className="latest-job-chip"
        title="No BLAST jobs yet — click to submit your first search"
        data-state="empty"
      >
        <Search size={13} strokeWidth={1.5} className="latest-job-chip__icon" />
        <span className="latest-job-chip__label">
          <span className="latest-job-chip__primary">No jobs</span>
          <span className="latest-job-chip__title">Run your first search</span>
        </span>
        <ArrowRight size={11} strokeWidth={1.5} className="latest-job-chip__chev" />
      </Link>
    );
  }

  // Latest by updated_at (falls back to created_at if missing).
  const latest = [...jobs].sort((a, b) => {
    const ta = Date.parse(a.updated_at || a.created_at || "") || 0;
    const tb = Date.parse(b.updated_at || b.created_at || "") || 0;
    return tb - ta;
  })[0];

  const view = describeJob(latest);

  return (
    <Link
      to={`/blast/jobs/${encodeURIComponent(latest.job_id)}`}
      className="latest-job-chip"
      title={`${view.tooltip} — click for details`}
      data-state={view.tone}
    >
      <view.Icon
        size={13}
        strokeWidth={1.5}
        className={view.tone === "running" ? "latest-job-chip__icon spin" : "latest-job-chip__icon"}
      />
      <span className="latest-job-chip__label">
        <span className="latest-job-chip__primary">{view.label}</span>
        <span className="latest-job-chip__title">{shortTitle(latest)}</span>
      </span>
      <span className="latest-job-chip__time">{relativeTime(latest.updated_at || latest.created_at)}</span>
      <ArrowRight size={11} strokeWidth={1.5} className="latest-job-chip__chev" />
    </Link>
  );
}

type Tone = "running" | "ok" | "fail" | "queued";

interface JobView {
  Icon: LucideIcon;
  label: string;
  tone: Tone;
  tooltip: string;
}

function describeJob(job: BlastJobSummary): JobView {
  const status = (job.status || job.phase || "").toLowerCase();
  if (status.includes("complet") || status === "succeeded" || status === "done") {
    return {
      Icon: CheckCircle2,
      label: "Completed",
      tone: "ok",
      tooltip: `Latest result: ${job.program} · ${shortDb(job.db)}`,
    };
  }
  if (status.includes("fail") || status === "error") {
    return {
      Icon: AlertTriangle,
      label: "Failed",
      tone: "fail",
      tooltip: `Last job failed: ${job.error ?? job.status}`,
    };
  }
  if (status.includes("queue") || status.includes("pending")) {
    return {
      Icon: Clock,
      label: "Queued",
      tone: "queued",
      tooltip: `Queued: ${job.program} · ${shortDb(job.db)}`,
    };
  }
  return {
    Icon: Loader2,
    label: humanPhase(job.phase) || "Running",
    tone: "running",
    tooltip: `In progress: ${job.program} · ${shortDb(job.db)} (${job.phase || job.status})`,
  };
}

function humanPhase(phase?: string): string {
  if (!phase) return "";
  const p = phase.toLowerCase();
  if (p.includes("provision")) return "Provisioning";
  if (p.includes("download")) return "Downloading DB";
  if (p.includes("split")) return "Splitting";
  if (p.includes("run")) return "Running";
  return phase;
}

function shortDb(db: string): string {
  if (!db) return "?";
  const parts = db.split("/").filter(Boolean);
  return parts[parts.length - 1] || db;
}

function shortTitle(job: BlastJobSummary): string {
  const t = job.job_title || `${job.program} · ${shortDb(job.db)}`;
  return t.length > 36 ? `${t.slice(0, 33)}…` : t;
}

function relativeTime(iso: string | undefined): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const diff = (Date.now() - t) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86_400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86_400)}d ago`;
}
