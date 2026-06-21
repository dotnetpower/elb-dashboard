import { useState } from "react";
import { ArrowDownToLine, FileText } from "lucide-react";

import { StepLogSection } from "@/components/BlastStepTimeline";
import { useStickToBottom } from "@/hooks/useStickToBottom";

import type { BlastResultsState } from "./useBlastResultsState";

export interface ExecutionStepsCardProps {
  state: BlastResultsState;
}

export function ExecutionStepsCard({ state }: ExecutionStepsCardProps) {
  const {
    job,
    executionStepsJob,
    effectivePhase,
    subscriptionId,
    storageAccount,
    resourceGroup,
    clusterName,
  } = state;
  // Whether the live-log auto-follow is currently glued to the tail. When the
  // user scrolls up to read history this flips false and we surface a "jump to
  // latest" pill so they can re-arm following with one click.
  const [following, setFollowing] = useState(true);
  // Compose a content "version" that ticks whenever the run produces new
  // output: phase change, last-write timestamp, and the submitting step's
  // accumulated log line count. Each tick is a cue to scroll-to-bottom
  // (only when the user is still anchored near the bottom — see hook).
  const jobRecord = job as unknown as Record<string, unknown> | null;
  const stepsForVersion =
    (jobRecord?.output as
      | { steps?: Record<string, Record<string, unknown>> }
      | undefined)?.steps ?? {};
  const submittingLines =
    (stepsForVersion["submitting"]?.log_line_count as number | undefined) ?? 0;
  const updatedAt = (jobRecord?.updated_at as string | undefined) ?? "";
  const stickVersion = `${effectivePhase}|${updatedAt}|${submittingLines}`;
  const { scrollToTail } = useStickToBottom({
    version: stickVersion,
    enabled: Boolean(job),
    // Follow the active/failed step row (rendered by StepRow with this
    // attribute) rather than the document bottom — the timeline always keeps
    // the still-pending steps below the active one, so the page bottom is a
    // stack of empty pending rows, not the live log.
    anchorSelector: '[data-blast-follow-anchor="true"]',
    onFollowingChange: setFollowing,
  });

  if (!job) return null;
  return (
    <section className="glass-card" style={{ padding: "14px 16px" }}>
      <h3
        style={{
          margin: "0 0 10px 0",
          fontSize: 14,
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <FileText size={15} strokeWidth={1.5} /> Execution Steps
      </h3>
      <StepLogSection
        phase={effectivePhase}
        job={(executionStepsJob ?? job) as unknown as Record<string, unknown>}
        subscriptionId={subscriptionId}
        storageAccount={storageAccount}
        resourceGroup={resourceGroup}
        clusterName={clusterName}
      />
      {!following && (
        <button
          type="button"
          onClick={scrollToTail}
          className="blast-jump-latest"
          aria-label="Jump to latest log output"
        >
          <ArrowDownToLine size={14} strokeWidth={1.75} />
          Jump to latest
        </button>
      )}
    </section>
  );
}
