import { useMemo, useState } from "react";

import { FilePreview } from "@/components/BlastFilePreview";
import { useBlastJobLogStream } from "@/hooks/useBlastJobLogStream";

import { buildStepLog } from "./buildStepLog";
import {
  FAILURE_PHASES,
  PHASE_STEPS,
  SHIMMER_STYLE,
  type StepState,
} from "./constants";
import {
  inferFailedStepKey,
  isRecord,
} from "./predicates";
import { StepRow } from "./StepRow";
import { getTimelineStepState } from "./stepState";
import { useStepDurations } from "./useStepDurations";

export function StepLogSection({
  phase,
  job,
  subscriptionId,
  storageAccount,
  resourceGroup,
  clusterName,
}: {
  phase: string;
  job: Record<string, unknown>;
  subscriptionId: string;
  storageAccount: string;
  resourceGroup?: string;
  clusterName?: string;
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const customStatus =
    typeof job?.custom_status === "object" && job?.custom_status !== null
      ? (job.custom_status as Record<string, unknown>)
      : null;
  const output = isRecord(job?.output) ? job.output : null;
  const stepsData = (customStatus?.steps ??
    (output as Record<string, unknown>)?.steps ??
    {}) as Record<string, Record<string, unknown>>;
  const jobId = job?.job_id as string;
  const streamEnabled = Boolean(jobId && phase !== "completed" && !FAILURE_PHASES.has(phase));
  const liveStream = useBlastJobLogStream({
    jobId,
    enabled: streamEnabled,
    subscriptionId,
    resourceGroup,
    clusterName,
  });
  const liveLogsByPhase = useMemo(() => {
    // GA-style: keep generous per-phase history (2000 lines) and surface a
    // single "older lines trimmed" head marker only when we actually clip.
    // The previous 80-line cap looked like a tiny scroll-tail mid-page and
    // hid most of the run output.
    const LIVE_LOG_CAP = 2000;
    const grouped: Record<string, string[]> = {};
    for (const event of liveStream.events) {
      const key = event.phase || "running";
      const prefix = event.source === "k8s" && event.pod
        ? `[${event.pod}${event.container ? `/${event.container}` : ""}] `
        : event.stream === "stderr"
          ? "[stderr] "
          : "";
      grouped[key] = [...(grouped[key] ?? []), `${prefix}${event.line}`];
    }
    for (const key of Object.keys(grouped)) {
      const lines = grouped[key];
      if (lines.length > LIVE_LOG_CAP) {
        const dropped = lines.length - LIVE_LOG_CAP;
        grouped[key] = [
          `[… ${dropped.toLocaleString()} older line${dropped === 1 ? "" : "s"} trimmed]`,
          ...lines.slice(-LIVE_LOG_CAP),
        ];
      }
    }
    return grouped;
  }, [liveStream.events]);
  const uploadBlobName = resolveUploadQueryBlobName(stepsData.preparing, job);
  const configBlobName = resolveConfigBlobName(stepsData.configuring, jobId);

  const { getStepDuration } = useStepDurations({ phase, stepsData });

  const toggle = (key: string) => setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));

  const failedStepKey = inferFailedStepKey(phase, stepsData, output, customStatus);
  const failedStepIdx = failedStepKey
    ? PHASE_STEPS.findIndex((s) => s.key === failedStepKey)
    : -1;

  const renderStepExtra = (key: string, state: StepState, isOpen: boolean) => {
    if (!isOpen || state === "pending") return null;
    // Prepare: show input.fa with 1000 char limit (FASTA can be very large).
    if (
      key === "preparing" &&
      (state === "done" || state === "active") &&
      jobId &&
      subscriptionId &&
      storageAccount &&
      uploadBlobName
    ) {
      return (
        <FilePreview
          jobId={jobId}
          filename="input.fa"
          blobName={uploadBlobName}
          subscriptionId={subscriptionId}
          storageAccount={storageAccount}
          resourceGroup={resourceGroup}
          maxBytes={1000}
        />
      );
    }
    // Configure: show full config (INI files are small).
    if (
      key === "configuring" &&
      (state === "done" || state === "active") &&
      jobId &&
      subscriptionId &&
      storageAccount
    ) {
      return (
        <FilePreview
          jobId={jobId}
          filename="elastic-blast.ini"
          blobName={configBlobName}
          subscriptionId={subscriptionId}
          storageAccount={storageAccount}
          resourceGroup={resourceGroup}
          maxBytes={10000}
        />
      );
    }
    return null;
  };

  return (
    <>
      <style>{SHIMMER_STYLE}</style>
      <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
        {PHASE_STEPS.map((step, i) => {
          const state = getTimelineStepState({
            phase,
            idx: i,
            key: step.key,
            stepsData,
            failedStepIdx,
          });
          const isOpen = expanded[step.key] ?? (state === "active" || state === "error");
          const log = buildStepLog({
            key: step.key,
            state,
            sd: stepsData[step.key] || {},
            output,
            customStatus,
            job,
            stepsData,
            jobId,
          });
          const liveLog = (liveLogsByPhase[step.key] ?? []).join("\n").trim();
          const combinedLog = liveLog
            ? `${log ? `${log}\n\n` : ""}--- Live Stream ---\n${liveLog}`
            : log;
          const duration = getStepDuration(step.key, state);
          const extra = renderStepExtra(step.key, state, isOpen);
          const subProgress = resolveStepSubProgress(stepsData[step.key]);
          return (
            <StepRow
              key={step.key}
              step={step}
              state={state}
              isOpen={isOpen}
              log={combinedLog}
              duration={duration}
              extra={extra}
              subProgress={subProgress}
              onToggle={() => toggle(step.key)}
            />
          );
        })}
      </div>
    </>
  );
}

function resolveUploadQueryBlobName(
  uploadStep: Record<string, unknown> | undefined,
  job: Record<string, unknown>,
): string | undefined {
  const payload = isRecord(job.payload) ? job.payload : null;
  const jobId = stringValue(job.job_id);
  const candidate =
    stringValue(uploadStep?.blob_path) ||
    stringValue(payload?.query_file) ||
    stringValue(payload?.query_blob_url);
  return candidate || (jobId ? `queries/${jobId}/input.fa` : undefined);
}

function resolveConfigBlobName(
  configureStep: Record<string, unknown> | undefined,
  jobId: string,
): string | undefined {
  const candidate =
    stringValue(configureStep?.config_blob_path) ||
    stringValue(configureStep?.config_url);
  return candidate || (jobId ? `queries/${jobId}/elastic-blast.ini` : undefined);
}

function stringValue(value: unknown): string {
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

function resolveStepSubProgress(
  stepData: Record<string, unknown> | undefined,
): { index: number; total: number; label?: string } | null {
  if (!stepData) return null;
  const raw = stepData.submit_progress;
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  const index = typeof obj.index === "number" ? obj.index : Number(obj.index);
  const total = typeof obj.total === "number" ? obj.total : Number(obj.total);
  if (!Number.isFinite(index) || !Number.isFinite(total) || total <= 0) return null;
  const label = typeof obj.label === "string" ? obj.label : undefined;
  return { index, total, label };
}
