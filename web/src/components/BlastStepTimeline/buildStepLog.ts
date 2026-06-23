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
  if (state === "skipped") {
    const decision = stringValue(sd.decision);
    const reason = stringValue(sd.skip_reason);
    const outputText = stringValue(sd.output) || stringValue(sd.last_output);
    if (decision === "warmed_ssd_reused") {
      return `Stage skipped: node-local SSD warmup is already ready.${
        outputText ? `\n\n--- Decision ---\n${outputText}` : ""
      }`;
    }
    return `Skipped${reason ? `: ${reason}` : ""}.${
      outputText ? `\n\n--- Details ---\n${outputText}` : ""
    }`;
  }

  const failureText =
    state === "error" ? getFailureText(sd, output, customStatus, job) : "";
  const stepLabel = PHASE_STEPS.find((step) => step.key === key)?.label ?? "Step";

  switch (key) {
    case "preparing": {
      const payload = isRecord(job.payload) ? job.payload : null;
      const bp =
        stringValue(sd.blob_path) ||
        stringValue(payload?.query_file) ||
        stringValue(payload?.query_blob_url);
      const prepareLog = (
        (sd.output as string) ||
        (sd.last_output as string) ||
        ""
      ).trim();
      if (state === "error") return `✗ ${stepLabel} failed:\n${failureText}`;
      if (prepareLog)
        return `${state === "done" ? "✓ Run prepared." : "Preparing run..."}\n\n--- Prepare Log ---\n${prepareLog}`;
      if (state === "done" && bp) return `✓ Run prepared. Query: ${queryDisplayPath(bp)}`;
      return state === "done"
        ? "✓ Run prepared."
        : "Validating submit inputs and resolving query/database metadata...";
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
        return `✓ Node-local DB warmup is ready.\n${
          wo ? `\n--- Console Output ---\n${wo}` : ""
        }`;
      if (state === "done")
        return `✓ Warmup check completed.\n${wo ? `\n--- Console Output ---\n${wo}` : ""}`;
      return "Checking whether DB shards are already warm on node-local SSD...";
    }
    case "staging_db": {
      const stageOutput = (
        (sd.output as string) ||
        (sd.last_output as string) ||
        ""
      ).trim();
      if (state === "error") return `✗ DB staging failed:\n${stageOutput || failureText}`;
      if (stageOutput)
        return `${state === "done" ? "✓ DB staging finished." : "Staging DB on node-local SSD..."}\n\n--- Live Console Output ---\n${stageOutput}`;
      return state === "done"
        ? "✓ DB shards are ready on node-local SSD."
        : "ElasticBLAST is reusing warm shards or staging any missing DB files on node-local SSD...";
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
      if (state === "done" && liveOutput)
        return `✓ Submitted successfully.\n\n--- Console Output ---\n${liveOutput}`;
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
        // Prefer the captured K8s pod log tail if present; show a polls/elapsed
        // summary only when we actually have those numbers. The previous
        // `polls ?? "?"` template surfaced "BLAST completed after ? polls
        // (~0s)" for short runs whose state row never carried a `polls` field
        // — that was misleading. Fall back to the K8s duration which is
        // always present once the runtime step is closed.
        const polls = rd.polls as number | undefined;
        const durationMs = rd.duration_ms as number | undefined;
        const elapsedFromPolls =
          typeof polls === "number" ? polls * 30 : undefined;
        const elapsedSec =
          typeof durationMs === "number"
            ? Math.max(1, Math.round(durationMs / 1000))
            : elapsedFromPolls;
        const pollsInfo =
          typeof polls === "number" && polls > 0 ? ` after ${polls} polls` : "";
        const elapsedInfo =
          typeof elapsedSec === "number" ? ` (~${elapsedSec}s)` : "";
        const lo = (rd.last_output as string | undefined)?.trim();
        let msg = `✓ BLAST completed${pollsInfo}${elapsedInfo}.`;
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
      const verifyInfo = verifyAttempts ? ` (${verifyAttempts} verification polls)` : "";
      if (state === "error") return `✗ Export failed:\n${eo || failureText}`;
      if (state === "done" && ed.success) {
        // Only render the "--- Export Log ---" block when there IS actual
        // export output. Many short runs leave `eo` empty; showing
        // "--- Export Log ---\n(no output)" looked like a missing capture
        // instead of "nothing to log".
        const tail = eo ? `\n\n--- Export Log ---\n${eo}` : "";
        return `✓ Results exported.${verifyInfo}\n${outInfo}${tail}`;
      }
      if (state === "done" && ed.auth_failed)
        return `⚠ Export partially failed: VM az login expired.\n${outInfo}\nResults written by AKS pods directly may still be available.\n\n--- Export Log ---\n${eo || ""}`;
      if (state === "done")
        return `✓ Export step completed.${verifyInfo}\n${outInfo}${
          eo ? `\n\n--- Export Log ---\n${eo}` : ""
        }`;
      if (verifyAttempts)
        return `Verifying result blobs... (attempt ${verifyAttempts})${
          liveExport ? `\n\n--- Export Verification Log ---\n${liveExport}` : ""
        }`;
      if (state === "active" && liveExport)
        return `Waiting for results-export K8s job + capturing pod logs...\n\n--- Export Verification Log ---\n${liveExport}`;
      return "Waiting for results-export K8s job + capturing pod logs...";
    }
    case "completed": {
      if (state === "error") return `✗ Completion failed:\n${failureText}`;
      const totalPolls = (stepsData.running?.polls as number) || 0;
      const completionOutput = ((sd.output as string) || (sd.last_output as string) || "").trim();
      // Prefer the backend-provided results prefix (date-tiered when the
      // layout flag is on) over a reconstructed flat `{jobId}/` hint.
      const resultsPrefix =
        (job.infrastructure as { results_prefix?: string } | undefined)?.results_prefix ||
        `${jobId}/`;
      return `✓ All steps completed.\n\n  Total polling time: ~${totalPolls * 30}s\n  Results container: results/${resultsPrefix}${
        completionOutput ? `\n\n--- Completion Log ---\n${completionOutput}` : ""
      }`;
    }
    default:
      return null;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringValue(value: unknown): string {
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

function queryDisplayPath(value: string): string {
  const raw = value.trim();
  if (raw.startsWith("queries/")) return raw;
  try {
    const parsed = new URL(raw.startsWith("az://") ? `https://${raw.slice(5)}` : raw);
    const parts = parsed.pathname.replace(/^\//, "").split("/");
    if (parts[0] === "queries" && parts.length > 1) {
      return `queries/${parts.slice(1).join("/")}`;
    }
  } catch {
    // Not a URL; treat it as a queries-container relative path.
  }
  return `queries/${raw.replace(/^\/+/, "")}`;
}
