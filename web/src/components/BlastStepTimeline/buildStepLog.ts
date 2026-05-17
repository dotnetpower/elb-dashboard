import { PHASE_STEPS, type StepState } from "./constants";
import { getFailureText } from "./predicates";

/**
 * Build the human-readable log block text for one orchestrator step.
 *
 * Returns `null` for pending steps. Pure function: all inputs are passed
 * in so the caller (StepLogSection) can memoise / cache without owning
 * any of this branchy display logic.
 */
export function buildStepLog({
  key,
  state,
  sd,
  output,
  customStatus,
  job,
  stepsData,
  jobId,
}: {
  key: string;
  state: StepState;
  sd: Record<string, unknown>;
  output: Record<string, unknown> | null;
  customStatus: Record<string, unknown> | null;
  job: Record<string, unknown>;
  stepsData: Record<string, Record<string, unknown>>;
  jobId: string;
}): string | null {
  if (state === "pending") return null;
  if (state === "skipped") return "⊘ Skipped — previous step failed.";

  const failureText =
    state === "error" ? getFailureText(sd, output, customStatus, job) : "";
  const stepLabel = PHASE_STEPS.find((step) => step.key === key)?.label ?? "Step";

  switch (key) {
    case "checking_vm": {
      const ps = sd.power_state as string;
      const started = sd.started as boolean;
      const vmLog = ((sd.output as string) || (sd.last_output as string) || "").trim();
      if (state === "error") return `✗ ${stepLabel} failed:\n${failureText}`;
      if (vmLog)
        return `${state === "done" ? "✓ VM ready." : "Checking VM..."}\n\n--- VM Check Log ---\n${vmLog}`;
      if (state === "done")
        return started
          ? `✓ VM was deallocated → started (power: ${ps || "running"}). Waited 30s for boot.`
          : `✓ VM already running (power: ${ps || "running"}).`;
      return "Checking Terminal sidecar reachability...";
    }
    case "enabling_storage":
      if (state === "error") return `✗ ${stepLabel} failed:\n${failureText}`;
      if (sd.output || sd.last_output) {
        return `${state === "done" ? "✓ Storage access configured." : "Configuring storage access..."}\n\n--- Storage Log ---\n${(
          (sd.output as string) || (sd.last_output as string)
        ).trim()}`;
      }
      return state === "done"
        ? "✓ Storage access configured for data transfer."
        : "Configuring storage network access...";
    case "uploading": {
      const bp = sd.blob_path as string;
      const uploadLog = (
        (sd.output as string) ||
        (sd.last_output as string) ||
        ""
      ).trim();
      if (state === "error") return `✗ ${stepLabel} failed:\n${failureText}`;
      if (uploadLog)
        return `${state === "done" ? "✓ Query uploaded." : "Uploading query..."}\n\n--- Upload Log ---\n${uploadLog}`;
      if (sd.skipped) return "✓ Query already uploaded (no inline data).";
      if (state === "done" && bp) return `✓ Query uploaded → ${bp}`;
      return state === "done"
        ? `✓ Query uploaded to queries/${jobId}/input.fa`
        : "Uploading FASTA query sequence...";
    }
    case "configuring": {
      const cu = sd.config_url as string;
      const configLog = (
        (sd.output as string) ||
        (sd.last_output as string) ||
        ""
      ).trim();
      if (state === "error") return `✗ ${stepLabel} failed:\n${failureText}`;
      if (configLog)
        return `${state === "done" ? "✓ Config generated." : "Generating config..."}\n\n--- Config Log ---\n${configLog}`;
      return state === "done"
        ? `✓ Config generated and uploaded.\n   ${cu || `queries/${jobId}/elastic-blast.ini`}`
        : "Generating elastic-blast INI configuration...";
    }
    case "warming_up": {
      const wo = ((sd.output as string) || (sd.last_output as string) || "").trim();
      if (state === "error") return `✗ Warmup failed:\n${wo || failureText}`;
      if (state === "done" && sd.success)
        return `✓ Cluster warmed up — DB shards loaded on local SSD.\n${
          wo ? `\n--- Console Output ---\n${wo}` : ""
        }`;
      if (state === "done")
        return `✓ Warmup step completed.\n${
          wo ? `\n--- Console Output ---\n${wo}` : ""
        }`;
      return "Running elastic-blast prepare — downloading DB shards to node SSDs...";
    }
    case "submitting": {
      const so =
        (sd.output as string) ||
        (sd.last_output as string) ||
        ((output as Record<string, unknown>)?.error as string);
      const liveOutput = (
        (sd.last_output as string) ||
        (sd.output as string) ||
        ""
      ).trim();
      const submitJobName = sd.submit_job_name as string | undefined;
      const pollAttempt = sd.poll_attempt as number | undefined;
      if (state === "error") return `✗ Submit failed:\n${so || failureText}`;
      if (state === "done" && sd.output)
        return `✓ Submitted successfully.\n\n--- Console Output ---\n${sd.output as string}`;
      if (state === "active" && liveOutput) {
        const meta = [
          submitJobName ? `helper job : ${submitJobName}` : null,
          pollAttempt ? `log poll   : #${pollAttempt}` : null,
        ]
          .filter(Boolean)
          .join("\n  ");
        return `Running elastic-blast submit...${meta ? `\n\n  ${meta}` : ""}\n\n--- Live Console Output ---\n${liveOutput}`;
      }
      return state === "done"
        ? "✓ Job submitted to AKS cluster."
        : "Starting elastic-blast submit helper job...";
    }
    case "running": {
      const blastStatus = customStatus?.blast_status as string;
      const pollAttempt = customStatus?.poll_attempt as number;
      const rd = sd as Record<string, unknown>;
      if (state === "active" && blastStatus) {
        const liveOutput = (rd.last_output as string | undefined)?.trim();
        return `Polling elastic-blast status...\n\n  BLAST status : ${blastStatus}\n  Poll attempt : #${pollAttempt ?? "?"}  (~${(pollAttempt ?? 0) * 30}s elapsed)${
          liveOutput ? `\n\n--- Live Status Output ---\n${liveOutput}` : ""
        }`;
      }
      if (state === "error") return `✗ BLAST run failed:\n${failureText}`;
      if (state === "done") {
        const polls = rd.polls as number;
        const lo = rd.last_output as string;
        let msg = `✓ BLAST completed after ${polls ?? "?"} polls (~${(polls ?? 0) * 30}s).`;
        if (lo) msg += `\n\n--- Last Status Output ---\n${lo}`;
        return msg;
      }
      return "Waiting for BLAST search to complete...";
    }
    case "exporting_results": {
      const ed = sd as Record<string, unknown>;
      const eo = ed.output as string;
      const liveExport = ed.last_output as string | undefined;
      const hasOut = ed.has_output_files as boolean | undefined;
      const verifyData = stepsData.result_verification as
        | Record<string, unknown>
        | undefined;
      const verifyAttempts = verifyData?.verify_attempts as number | undefined;
      const outInfo =
        hasOut !== undefined
          ? hasOut
            ? "✓ .out result files found in blob."
            : "⚠ No .out result files detected yet."
          : "";
      const verifyInfo = verifyAttempts
        ? ` (${verifyAttempts} verification polls)`
        : "";
      if (state === "error") return `✗ Export failed:\n${eo || failureText}`;
      if (state === "done" && ed.success)
        return `✓ Results exported.${verifyInfo}\n${outInfo}\n\n--- Export Log ---\n${eo || "(no output)"}`;
      if (state === "done" && ed.auth_failed)
        return `⚠ Export partially failed: VM az login expired.\n${outInfo}\nResults written by AKS pods directly may still be available.\n\n--- Export Log ---\n${eo || ""}`;
      if (state === "done")
        return `✓ Export step completed.${verifyInfo}\n${outInfo}${
          eo ? `\n\n--- Export Log ---\n${eo}` : ""
        }`;
      if (verifyAttempts)
        return `Verifying result blobs... (attempt ${verifyAttempts})${
          liveExport
            ? `\n\n--- Export Verification Log ---\n${liveExport}`
            : ""
        }`;
      if (state === "active" && liveExport)
        return `Waiting for results-export K8s job + capturing pod logs...\n\n--- Export Verification Log ---\n${liveExport}`;
      return "Waiting for results-export K8s job + capturing pod logs...";
    }
    case "completed": {
      if (state === "error") return `✗ Completion failed:\n${failureText}`;
      const totalPolls = (stepsData.running?.polls as number) || 0;
      return `✓ All steps completed.\n\n  Total polling time: ~${totalPolls * 30}s\n  Results container: results/${jobId}/`;
    }
    default:
      return null;
  }
}
