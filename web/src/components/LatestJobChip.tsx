import { Link } from "react-router-dom";
import { ArrowRight, Search } from "lucide-react";

import { useLatestBlastJob } from "@/hooks/useLatestBlastJob";
import {
  describeBlastJob,
  shortBlastJobTitle,
} from "@/lib/blastJobView";
import { formatRelativeTime } from "@/lib/relativeTime";

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
 * This file owns presentation only. Data acquisition lives in
 * `useLatestBlastJob`; domain → view-model mapping lives in
 * `lib/blastJobView`; time formatting lives in `lib/relativeTime`.
 */
export function LatestJobChip() {
  const { job, isLoading, isError } = useLatestBlastJob();

  if (isLoading || isError) return null;

  if (job === null) {
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

  const view = describeBlastJob(job);
  const iconClass =
    view.tone === "running"
      ? "latest-job-chip__icon spin"
      : "latest-job-chip__icon";

  return (
    <Link
      to={`/blast/jobs/${encodeURIComponent(job.job_id)}`}
      className="latest-job-chip"
      title={`${view.tooltip} — click for details`}
      data-state={view.tone}
    >
      <view.Icon size={13} strokeWidth={1.5} className={iconClass} />
      <span className="latest-job-chip__label">
        <span className="latest-job-chip__primary">{view.label}</span>
        <span className="latest-job-chip__title">{shortBlastJobTitle(job)}</span>
      </span>
      <span className="latest-job-chip__time">
        {formatRelativeTime(job.updated_at || job.created_at)}
      </span>
      <ArrowRight size={11} strokeWidth={1.5} className="latest-job-chip__chev" />
    </Link>
  );
}
