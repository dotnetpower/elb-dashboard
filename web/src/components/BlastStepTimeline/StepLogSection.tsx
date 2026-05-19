import { useState } from "react";

import { FilePreview } from "@/components/BlastFilePreview";

import { buildStepLog } from "./buildStepLog";
import {
  FAILURE_PHASES,
  PHASE_STEPS,
  PHASE_TO_STEP,
  SHIMMER_STYLE,
  type StepState,
} from "./constants";
import {
  inferFailedStepKey,
  isRecord,
  stepHasEvidence,
  stepHasFailure,
} from "./predicates";
import { StepRow } from "./StepRow";
import { useStepDurations } from "./useStepDurations";

export function StepLogSection({
  phase,
  job,
  subscriptionId,
  storageAccount,
}: {
  phase: string;
  job: Record<string, unknown>;
  subscriptionId: string;
  storageAccount: string;
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
  const uploadBlobName = resolveUploadQueryBlobName(stepsData.uploading, job);
  const configBlobName = resolveConfigBlobName(stepsData.configuring, jobId);

  const { getStepDuration } = useStepDurations({ phase, stepsData });

  const toggle = (key: string) => setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));

  const effectivePhaseKey = PHASE_TO_STEP[phase] ?? phase;
  const currentPhaseIdx = PHASE_STEPS.findIndex((s) => s.key === effectivePhaseKey);
  const failedStepKey = inferFailedStepKey(phase, stepsData, output, customStatus);
  const failedStepIdx = failedStepKey
    ? PHASE_STEPS.findIndex((s) => s.key === failedStepKey)
    : -1;

  const getStepState = (idx: number, key: string): StepState => {
    if (phase === "completed") return "done";
    if (FAILURE_PHASES.has(phase)) {
      if (failedStepIdx >= 0) {
        if (idx < failedStepIdx) return "done";
        if (idx === failedStepIdx) return "error";
        return "skipped";
      }
      if (stepHasFailure(stepsData[key])) return "error";
      if (stepHasEvidence(stepsData[key])) return "done";
      return "skipped";
    }
    if (currentPhaseIdx < 0) return "pending";
    if (idx < currentPhaseIdx) return "done";
    if (idx === currentPhaseIdx) return "active";
    return "pending";
  };

  const renderStepExtra = (key: string, state: StepState, isOpen: boolean) => {
    if (!isOpen || state === "pending") return null;
    // Upload: show input.fa with 1000 char limit (FASTA can be very large).
    if (
      key === "uploading" &&
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
          const state = getStepState(i, step.key);
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
          const duration = getStepDuration(step.key, state);
          const extra = renderStepExtra(step.key, state, isOpen);
          return (
            <StepRow
              key={step.key}
              step={step}
              state={state}
              isOpen={isOpen}
              log={log}
              duration={duration}
              extra={extra}
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
