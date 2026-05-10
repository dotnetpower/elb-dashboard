import { useState, useEffect, useRef } from "react";
import { useParams, useSearchParams, Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Download, ArrowLeft, RefreshCw, Copy, Check, CheckCircle2, Loader2,
  Server, HardDrive, Upload, Settings, Send, Dna, Package, Trophy,
  Clock, XCircle, FileText, AlertTriangle, Unlock, FolderOpen,
  ChevronRight, ChevronDown, StopCircle,
} from "lucide-react";

import { blastApi, type BlastResultFile } from "@/api/endpoints";
import { api } from "@/api/client";
import { loadSavedConfig } from "@/components/SetupWizard";
import { statusColor } from "@/constants";
import { useToast } from "@/components/Toast";
import { ConfirmDialog } from "@/components/ConfirmDialog";

// Shimmer animation for active steps
const shimmerStyle = `
@keyframes step-shimmer {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(300%); }
}
`;

// Phase steps matching orchestrator order exactly
const PHASE_STEPS = [
  { key: "checking_vm", label: "Prepare VM", desc: "Start remote terminal", icon: Server },
  { key: "enabling_storage", label: "Open Storage", desc: "Enable public access", icon: HardDrive },
  { key: "uploading", label: "Upload Query", desc: "Send sequence to blob", icon: Upload },
  { key: "configuring", label: "Configure", desc: "Generate INI config", icon: Settings },
  { key: "warming_up", label: "Warmup", desc: "Prepare DB shards on SSD", icon: Dna },
  { key: "submitting", label: "Submit Job", desc: "Send to AKS cluster", icon: Send },
  { key: "running", label: "BLAST Run", desc: "Sequence alignment", icon: Dna },
  { key: "exporting_results", label: "Export", desc: "Copy results to blob", icon: Package },
  { key: "completed", label: "Complete", desc: "All done!", icon: Trophy },
];

const PHASE_MESSAGES: Record<string, string> = {
  checking_vm: "Verifying Remote Terminal VM is running...",
  enabling_storage: "Enabling storage public access for data transfer...",
  uploading: "Uploading query sequence to Azure Blob Storage...",
  configuring: "Generating ElasticBLAST configuration...",
  warming_up: "Preparing cluster with DB shards on local SSD (warmup)...",
  warmup_failed: "Cluster warmup failed.",
  submitting: "Submitting job to AKS cluster...",
  running: "BLAST search is running on the cluster...",
  exporting_results: "Verifying result files and exporting logs from cluster...",
  completed: "Job completed successfully!",
  failed: "Job failed.",
  error: "An error occurred.",
};

function ElapsedTimer({ startTime }: { startTime: string }) {
  const [elapsed, setElapsed] = useState("");
  useEffect(() => {
    const start = new Date(startTime).getTime();
    const tick = () => {
      const diff = Math.max(0, Date.now() - start);
      const s = Math.floor(diff / 1000);
      const m = Math.floor(s / 60);
      const h = Math.floor(m / 60);
      if (h > 0) setElapsed(`${h}h ${m % 60}m ${s % 60}s`);
      else if (m > 0) setElapsed(`${m}m ${s % 60}s`);
      else setElapsed(`${s}s`);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [startTime]);
  return <span style={{ fontVariantNumeric: "tabular-nums" }}>{elapsed}</span>;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

// --- Syntax-highlighted file previews ---
function HighlightedINI({ text }: { text: string }) {
  return (
    <pre style={{
      margin: 0, padding: "8px 10px", borderRadius: 4,
      background: "rgba(0,0,0,0.25)", fontSize: 11, lineHeight: 1.6,
      maxHeight: 220, overflowY: "auto", whiteSpace: "pre-wrap", wordBreak: "break-all",
    }}>
      {text.split("\n").map((line, i) => {
        if (line.startsWith("[")) return <div key={i} style={{ color: "var(--accent)", fontWeight: 600 }}>{line}</div>;
        const eq = line.indexOf("=");
        if (eq > 0 && !line.startsWith("#")) {
          return <div key={i}><span style={{ color: "#9aa3b8" }}>{line.slice(0, eq)}</span><span style={{ color: "var(--text-faint)" }}>=</span><span style={{ color: "var(--text-primary)" }}>{line.slice(eq + 1)}</span></div>;
        }
        return <div key={i} style={{ color: "var(--text-faint)" }}>{line}</div>;
      })}
    </pre>
  );
}

function HighlightedFASTA({ text }: { text: string }) {
  const colorMap: Record<string, string> = { A: "#6ad6a3", T: "#e07b8a", G: "#f0c674", C: "#7aa7ff", U: "#e07b8a" };
  return (
    <pre style={{
      margin: 0, padding: "8px 10px", borderRadius: 4,
      background: "rgba(0,0,0,0.25)", fontSize: 11, lineHeight: 1.6,
      maxHeight: 180, overflowY: "auto", whiteSpace: "pre-wrap", wordBreak: "break-all",
    }}>
      {text.split("\n").map((line, i) => {
        if (line.startsWith(">")) return <div key={i} style={{ color: "var(--accent)", fontWeight: 600 }}>{line}</div>;
        return <div key={i}>{[...line].map((ch, j) => <span key={j} style={{ color: colorMap[ch.toUpperCase()] || "var(--text-faint)" }}>{ch}</span>)}</div>;
      })}
    </pre>
  );
}

function FilePreview({ jobId, filename, subscriptionId, storageAccount, maxBytes }: {
  jobId: string; filename: string; subscriptionId: string; storageAccount: string; maxBytes?: number;
}) {
  const q = useQuery({
    queryKey: ["blast-file", jobId, filename],
    queryFn: () => blastApi.readJobFile(jobId, filename, subscriptionId, storageAccount, maxBytes ?? 4096),
    staleTime: Infinity,
  });
  if (q.isLoading) return <span className="muted"><Loader2 size={12} className="spin" style={{ verticalAlign: "middle" }} /> Loading {filename}...</span>;
  if (q.isError) return <span className="muted" style={{ fontSize: 11 }}>Could not load {filename}</span>;
  const content = q.data?.content ?? "";
  const truncated = q.data?.truncated;
  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 4, display: "flex", alignItems: "center", gap: 6 }}>
        <FileText size={11} /> {filename}
        {truncated && (
          <span style={{
            fontSize: 10, padding: "1px 6px", borderRadius: 3,
            background: "rgba(240,198,116,0.12)", color: "var(--warning)",
          }}>
            Showing first {(maxBytes ?? 4096).toLocaleString()} chars — file may be longer
          </span>
        )}
      </div>
      {filename.endsWith(".ini") ? <HighlightedINI text={content} /> :
       filename.endsWith(".fa") || filename.endsWith(".fasta") ? <HighlightedFASTA text={content} /> :
       <pre style={{ margin: 0, padding: "8px 10px", borderRadius: 4, background: "rgba(0,0,0,0.25)", fontSize: 11, lineHeight: 1.5, maxHeight: 200, overflowY: "auto", whiteSpace: "pre-wrap", color: "var(--text-muted)" }}>{content}</pre>}
    </div>
  );
}

// --- Collapsible Step Log (GitHub Actions style) ---
type StepState = "done" | "active" | "pending" | "error" | "skipped";

// Premium log block with line numbers, syntax coloring, scrolling, and copy
function StepLogBlock({ log, state, stepKey }: { log: string; state: StepState; stepKey: string }) {
  const [copied, setCopied] = useState(false);
  const [isExpanded, setIsExpanded] = useState(false);

  // Split into summary (first line) and detail (rest with line numbers)
  const delimIdx = log.indexOf("---");
  const hasSections = delimIdx > 0;
  const allLines = log.split("\n");
  // Summary = text before the first "---" section, or first line if multi-line without "---"
  const summary = hasSections
    ? log.slice(0, delimIdx).trim()
    : allLines.length <= 2 ? log : allLines[0];
  // Detail = everything from "---" onward, or lines 2+ if no "---" but multi-line
  const detail = hasSections
    ? log.slice(delimIdx).trim()
    : allLines.length > 2 ? allLines.slice(1).join("\n") : null;
  const detailLines = detail?.split("\n") ?? [];
  const isLong = detailLines.length > 40;

  const copyLog = () => {
    navigator.clipboard.writeText(log).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="step-log-block" data-state={state}>
      {/* Summary line */}
      <div className="step-log-summary">
        <span>{summary}</span>
        <button className="step-log-copy" onClick={copyLog} title="Copy full log">
          {copied ? <Check size={11} strokeWidth={2} /> : <Copy size={11} strokeWidth={1.5} />}
          <span>{copied ? "Copied" : "Copy"}</span>
        </button>
      </div>

      {/* Detail/console output */}
      {detail && (
        <div className={`step-log-detail${isLong && !isExpanded ? " step-log-detail--collapsed" : ""}`}>
          <div className="step-log-lines">
            {(isLong && !isExpanded ? detailLines.slice(0, 40) : detailLines).map((line, i) => {
              let lineClass = "step-log-text";
              if (line.startsWith("WARNING") || line.startsWith("⚠")) lineClass += " step-log-text--warn";
              else if (line.startsWith("ERROR") || line.startsWith("✗") || /ErrorCode:|<Error>|ContainerNotFound|FATAL/.test(line)) lineClass += " step-log-text--error";
              else if (line.startsWith("✓") || line.includes("=ok") || line.includes("EXIT_CODE=0")) lineClass += " step-log-text--ok";
              else if (line.startsWith("---")) lineClass += " step-log-text--header";
              else if (line.startsWith("INFO:")) lineClass += " step-log-text--info";
              return (
                <div key={`${stepKey}-${i}`} className="step-log-line">
                  <span className="step-log-ln">{i + 1}</span>
                  <span className={lineClass}>{line || "\u00A0"}</span>
                </div>
              );
            })}
          </div>
          {isLong && !isExpanded && (
            <button className="step-log-expand" onClick={() => setIsExpanded(true)}>
              Show all {detailLines.length} lines
            </button>
          )}
          {isLong && isExpanded && (
            <button className="step-log-expand" onClick={() => setIsExpanded(false)}>
              Collapse
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function StepLogSection({ phase, job, subscriptionId, storageAccount, resourceGroup: _resourceGroup }: {
  phase: string; job: Record<string, unknown>;
  subscriptionId: string; storageAccount: string; resourceGroup: string;
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [, setTick] = useState(0);
  const phaseTimestamps = useRef<Record<string, number>>({});
  const phaseDurations = useRef<Record<string, number>>({});
  const customStatus = (typeof job?.custom_status === "object" && job?.custom_status !== null)
    ? (job.custom_status as Record<string, unknown>) : null;
  const output = job?.output as Record<string, unknown> | null;
  const stepsData = (customStatus?.steps ?? (output as Record<string, unknown>)?.steps ?? {}) as Record<string, Record<string, unknown>>;
  const jobId = job?.job_id as string;

  // Track phase transitions to calculate per-step durations
  useEffect(() => {
    if (!phase) return;
    const now = Date.now();
    const ts = phaseTimestamps.current;

    // Find the step key that matches the current phase
    const stepIdx = PHASE_STEPS.findIndex((s) => s.key === phase);
    if (stepIdx >= 0 && !ts[phase]) {
      ts[phase] = now;
      // Mark previous step as completed with duration
      if (stepIdx > 0) {
        const prevKey = PHASE_STEPS[stepIdx - 1].key;
        if (ts[prevKey] && !phaseDurations.current[prevKey]) {
          phaseDurations.current[prevKey] = now - ts[prevKey];
        }
      }
    }
    // If completed/failed, finalize all durations
    if (phase === "completed" || phase === "failed") {
      for (let i = 0; i < PHASE_STEPS.length; i++) {
        const key = PHASE_STEPS[i].key;
        if (ts[key] && !phaseDurations.current[key]) {
          const nextKey = PHASE_STEPS[i + 1]?.key;
          const endTime = nextKey && ts[nextKey] ? ts[nextKey] : now;
          phaseDurations.current[key] = endTime - ts[key];
        }
      }
    }
  }, [phase]);

  // Tick every second to update active step timer
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const getStepDuration = (key: string, state: StepState): string | null => {
    if (state === "pending" || state === "skipped") return null;
    const dur = phaseDurations.current[key];
    if (dur) {
      const s = Math.round(dur / 1000);
      return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
    }
    // Active step — show live elapsed
    if (state === "active") {
      const start = phaseTimestamps.current[key];
      if (start) {
        const elapsed = Math.round((Date.now() - start) / 1000);
        return elapsed >= 60 ? `${Math.floor(elapsed / 60)}m ${elapsed % 60}s` : `${elapsed}s`;
      }
    }
    return null;
  };

  const toggle = (key: string) => setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));

  // Map failure phases to their corresponding step key
  const PHASE_TO_STEP: Record<string, string> = {
    submit_failed: "submitting",
    warmup_failed: "warming_up",
  };
  const effectivePhaseKey = PHASE_TO_STEP[phase] ?? phase;
  const currentPhaseIdx = PHASE_STEPS.findIndex((s) => s.key === effectivePhaseKey);

  const getStepState = (idx: number, _key: string): StepState => {
    if (phase === "completed") return "done";
    if (phase === "failed" || phase === "error" || phase === "submit_failed" || phase === "warmup_failed") {
      if (currentPhaseIdx < 0) return "skipped"; // unknown phase
      if (idx < currentPhaseIdx) return "done";
      if (idx === currentPhaseIdx) return "error";
      return "skipped";
    }
    if (currentPhaseIdx < 0) return "pending";
    if (idx < currentPhaseIdx) return "done";
    if (idx === currentPhaseIdx) return "active";
    return "pending";
  };

  const getStepLog = (key: string, state: StepState): string | null => {
    if (state === "pending") return null;
    if (state === "skipped") return "⊘ Skipped — previous step failed.";
    const sd = stepsData[key] || {};
    switch (key) {
      case "checking_vm": {
        const ps = sd.power_state as string;
        const started = sd.started as boolean;
        if (state === "done") return started ? `✓ VM was deallocated → started (power: ${ps || "running"}). Waited 30s for boot.` : `✓ VM already running (power: ${ps || "running"}).`;
        return "Checking Remote Terminal VM power state...";
      }
      case "enabling_storage":
        return state === "done" ? "✓ Storage access configured for data transfer." : "Configuring storage network access...";
      case "uploading": {
        const bp = sd.blob_path as string;
        if (sd.skipped) return "✓ Query already uploaded (no inline data).";
        if (state === "done" && bp) return `✓ Query uploaded → ${bp}`;
        return state === "done" ? `✓ Query uploaded to queries/${jobId}/input.fa` : "Uploading FASTA query sequence...";
      }
      case "configuring": {
        const cu = sd.config_url as string;
        return state === "done" ? `✓ Config generated and uploaded.\n   ${cu || `queries/${jobId}/elastic-blast.ini`}` : "Generating elastic-blast INI configuration...";
      }
      case "warming_up": {
        const wo = sd.output as string;
        if (state === "error" && wo) return `✗ Warmup failed:\n${wo}`;
        if (state === "done" && sd.success) return `✓ Cluster warmed up — DB shards loaded on local SSD.\n${wo ? `\n--- Console Output ---\n${(wo).slice(0, 800)}` : ""}`;
        if (state === "done") return `✓ Warmup step completed.\n${wo ? `\n--- Console Output ---\n${wo.slice(0, 800)}` : ""}`;
        return "Running elastic-blast prepare — downloading DB shards to node SSDs...";
      }
      case "submitting": {
        const so = sd.output as string || (output as Record<string, unknown>)?.error as string;
        if (state === "error" && so) return `✗ Submit failed:\n${so}`;
        if (state === "done" && sd.output) return `✓ Submitted successfully.\n\n--- Console Output ---\n${(sd.output as string).slice(0, 600)}`;
        return state === "done" ? "✓ Job submitted to AKS cluster." : "Running elastic-blast submit on Remote Terminal VM...";
      }
      case "running": {
        const blastStatus = customStatus?.blast_status as string;
        const pollAttempt = customStatus?.poll_attempt as number;
        const rd = sd as Record<string, unknown>;
        if (state === "active" && blastStatus) {
          return `Polling elastic-blast status...\n\n  BLAST status : ${blastStatus}\n  Poll attempt : #${pollAttempt ?? "?"}  (~${(pollAttempt ?? 0) * 30}s elapsed)`;
        }
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
        const hasOut = ed.has_output_files as boolean | undefined;
        const verifyData = stepsData.result_verification as Record<string, unknown> | undefined;
        const verifyAttempts = verifyData?.verify_attempts as number | undefined;
        const outInfo = hasOut !== undefined
          ? (hasOut ? "✓ .out result files found in blob." : "⚠ No .out result files detected yet.")
          : "";
        const verifyInfo = verifyAttempts ? ` (${verifyAttempts} verification polls)` : "";
        if (state === "done" && ed.success) return `✓ Results exported.${verifyInfo}\n${outInfo}\n\n--- Export Log ---\n${eo || "(no output)"}`;
        if (state === "done" && ed.auth_failed) return `⚠ Export partially failed: VM az login expired.\n${outInfo}\nResults written by AKS pods directly may still be available.\n\n--- Export Log ---\n${eo || ""}`;
        if (state === "done") return `✓ Export step completed.${verifyInfo}\n${outInfo}${eo ? `\n\n--- Export Log ---\n${eo}` : ""}`;
        if (verifyAttempts) return `Verifying result blobs... (attempt ${verifyAttempts})`;
        return "Waiting for results-export K8s job + capturing pod logs...";
      }
      case "completed": {
        const totalPolls = (stepsData.running?.polls as number) || 0;
        return `✓ All steps completed.\n\n  Total polling time: ~${totalPolls * 30}s\n  Results container: results/${jobId}/`;
      }
      default:
        return null;
    }
  };

  const renderStepExtra = (key: string, state: StepState, isOpen: boolean) => {
    if (!isOpen || state === "pending") return null;
    // Upload: show input.fa with 1000 char limit (FASTA can be very large)
    if (key === "uploading" && (state === "done" || state === "active") && jobId && subscriptionId && storageAccount) {
      return <FilePreview jobId={jobId} filename="input.fa" subscriptionId={subscriptionId} storageAccount={storageAccount} maxBytes={1000} />;
    }
    // Configure: show full config (INI files are small)
    if (key === "configuring" && (state === "done" || state === "active") && jobId && subscriptionId && storageAccount) {
      return <FilePreview jobId={jobId} filename="elastic-blast.ini" subscriptionId={subscriptionId} storageAccount={storageAccount} maxBytes={10000} />;
    }
    return null;
  };

  return (
    <>
    <style>{shimmerStyle}</style>
    <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
      {PHASE_STEPS.map((s, i) => {
        const state = getStepState(i, s.key);
        const isOpen = expanded[s.key] ?? (state === "active" || state === "error");
        const log = getStepLog(s.key, state);
        const Icon = s.icon;

        const stateColor = state === "done" ? "var(--success)"
          : state === "active" ? "var(--accent)"
          : state === "error" ? "var(--danger)"
          : "var(--text-faint)";

        return (
          <div key={s.key} style={{
            borderRadius: 6, overflow: "hidden",
            background: isOpen ? "rgba(255,255,255,0.03)" : "transparent",
            border: isOpen ? "1px solid var(--border-weak)" : "1px solid transparent",
            opacity: state === "skipped" ? 0.5 : 1,
            position: "relative",
          }}>
            {/* Shimmer bar at top of active step */}
            {state === "active" && (
              <div style={{
                position: "absolute", top: 0, left: 0, right: 0, height: 2,
                overflow: "hidden", borderRadius: "6px 6px 0 0", pointerEvents: "none",
                background: "rgba(122,167,255,0.10)",
              }}>
                <div style={{
                  position: "absolute", top: 0, left: 0, width: "33%", height: "100%",
                  background: "linear-gradient(90deg, transparent 0%, var(--accent) 50%, transparent 100%)",
                  animation: "step-shimmer 1.2s linear infinite",
                }} />
              </div>
            )}
            <button
              onClick={() => state !== "pending" && state !== "skipped" && toggle(s.key)}
              disabled={state === "pending" || state === "skipped"}
              style={{
                all: "unset", display: "flex", alignItems: "center", gap: 10,
                width: "100%", padding: "8px 12px", cursor: state === "pending" ? "default" : "pointer",
                fontSize: 13, boxSizing: "border-box",
              }}
            >
              {/* Expand chevron */}
              <span style={{ color: "var(--text-faint)", width: 14, flexShrink: 0 }}>
                {state !== "pending" && state !== "skipped" ? (isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />) : null}
              </span>
              {/* Status icon */}
              <span style={{ flexShrink: 0 }}>
                {state === "done" && <CheckCircle2 size={16} style={{ color: "var(--success)" }} />}
                {state === "active" && <Loader2 size={16} className="spin" style={{ color: "var(--accent)" }} />}
                {state === "error" && <XCircle size={16} style={{ color: "var(--danger)" }} />}
                {state === "skipped" && <Icon size={15} style={{ color: "var(--text-faint)", opacity: 0.5 }} />}
                {state === "pending" && <Icon size={15} style={{ color: "var(--text-faint)", opacity: 0.4 }} />}
              </span>
              {/* Label */}
              <span style={{ flex: 1, color: stateColor, fontWeight: state === "active" ? 600 : 400 }}>
                {s.label}
                <span style={{ fontSize: 11, marginLeft: 8, color: "var(--text-faint)", fontWeight: 400 }}>
                  {state === "skipped" ? "Skipped" : s.desc}
                </span>
              </span>
              {/* Duration + Status badge */}
              {(() => {
                const dur = getStepDuration(s.key, state);
                return (
                  <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    {dur && (
                      <span style={{
                        fontSize: 11, color: state === "active" ? "var(--accent)" : "var(--text-faint)",
                        fontVariantNumeric: "tabular-nums", fontWeight: state === "active" ? 500 : 400,
                        minWidth: 28, textAlign: "right",
                      }}>
                        {dur}
                      </span>
                    )}
                    {state === "done" && (
                      <span style={{ fontSize: 11, color: "var(--success)" }}>✓</span>
                    )}
                    {state === "skipped" && (
                      <span style={{ fontSize: 10, color: "var(--text-faint)", padding: "1px 6px", background: "rgba(255,255,255,0.04)", borderRadius: 3 }}>
                        skipped
                      </span>
                    )}
                    {state === "error" && (
                      <span style={{ fontSize: 10, color: "var(--danger)", padding: "1px 6px", background: "rgba(224,123,138,0.08)", borderRadius: 3 }}>
                        failed
                      </span>
                    )}
                  </span>
                );
              })()}
            </button>
            {/* Collapsible log — premium CI-style */}
            {isOpen && log && <StepLogBlock log={log} state={state} stepKey={s.key} />}
            {/* Extra content: file previews */}
            {isOpen && renderStepExtra(s.key, state, isOpen) && (
              <div style={{
                padding: "8px 12px 10px 50px",
                borderTop: log ? "none" : "1px solid var(--border-weak)",
                background: "rgba(0,0,0,0.15)",
              }}>
                {renderStepExtra(s.key, state, isOpen)}
              </div>
            )}
          </div>
        );
      })}
    </div>
    </>
  );
}

function StorageLockedPanel({ subscriptionId, storageAccount, resourceGroup, jobId, onUnlocked }: {
  subscriptionId: string; storageAccount: string; resourceGroup: string; jobId: string;
  onUnlocked: () => void;
}) {
  const { toast } = useToast();
  const resultsUrl = `https://${storageAccount}.blob.core.windows.net/results/${jobId}`;

  const enableMutation = useMutation({
    mutationFn: () =>
      api.post<{ public_network_access: string | null }>(
        "/monitor/storage/public-access",
        { subscription_id: subscriptionId, resource_group: resourceGroup, account_name: storageAccount, enabled: true },
      ),
    onSuccess: () => {
      toast("Storage unlocked. Loading results...", "success");
      // Wait for propagation then refresh
      setTimeout(onUnlocked, 8000);
    },
    onError: (e) => toast(`Failed to enable storage: ${(e as Error).message}`, "error"),
  });

  return (
    <div style={{ marginTop: "var(--space-3)" }}>
      {/* Warning + action */}
      <div style={{
        padding: "16px", borderRadius: 10,
        background: "rgba(240,198,116,0.06)", border: "1px solid rgba(240,198,116,0.18)",
      }}>
        <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
          <AlertTriangle size={18} style={{ color: "var(--warning)", flexShrink: 0, marginTop: 2 }} />
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 14, fontWeight: 600, color: "var(--warning)", marginBottom: 6 }}>
              Storage public access is disabled
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5 }}>
              Result files are stored in your Azure Blob Storage but cannot be listed while public access is off.
              Temporarily enable it to view and download your BLAST results.
            </div>
            <button
              className="glass-button glass-button--primary"
              onClick={() => enableMutation.mutate()}
              disabled={enableMutation.isPending}
              style={{ marginTop: 12, display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13 }}
            >
              {enableMutation.isPending
                ? <><Loader2 size={14} className="spin" /> Enabling...</>
                : <><Unlock size={14} strokeWidth={1.5} /> Enable Storage &amp; Load Results</>}
            </button>
          </div>
        </div>
      </div>

      {/* Results location info */}
      <div style={{
        marginTop: "var(--space-3)", padding: "14px 16px", borderRadius: 10,
        background: "var(--bg-tertiary)", fontSize: 12,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
          <FolderOpen size={14} strokeWidth={1.5} style={{ color: "var(--text-muted)" }} />
          <span style={{ fontWeight: 600, color: "var(--text-primary)" }}>Results Location</span>
        </div>
        <div style={{
          display: "grid", gridTemplateColumns: "100px 1fr",
          gap: "4px 12px", color: "var(--text-muted)",
        }}>
          <span>Account</span>
          <code style={{ fontSize: 11, color: "var(--text-primary)" }}>{storageAccount}</code>
          <span>Container</span>
          <code style={{ fontSize: 11, color: "var(--text-primary)" }}>results</code>
          <span>Prefix</span>
          <code style={{ fontSize: 11, color: "var(--text-primary)" }}>{jobId}/</code>
          <span>URL</span>
          <code style={{ fontSize: 10, color: "var(--text-faint)", wordBreak: "break-all" }}>{resultsUrl}</code>
        </div>
      </div>
    </div>
  );
}

export function BlastResults() {
  const { jobId } = useParams<{ jobId: string }>();
  const [searchParams] = useSearchParams();
  const config = loadSavedConfig();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [copiedId, setCopiedId] = useState(false);
  const [downloadingFile, setDownloadingFile] = useState<string | null>(null);
  const [showCancelConfirm, setShowCancelConfirm] = useState(false);
  const prevPhaseRef = useRef<string | null>(null);
  const subscriptionId = searchParams.get("subscription_id") || config?.subscriptionId || "";
  const storageAccount = searchParams.get("storage_account") || config?.storageAccountName || "";
  const resourceGroup = config?.workloadResourceGroup || "";

  const jobQuery = useQuery({
    queryKey: ["blast-job", jobId],
    queryFn: () => blastApi.getJob(jobId!),
    enabled: Boolean(jobId),
    refetchInterval: (q) => {
      const d = q.state.data;
      if (!d) return 3_000;
      if (d.status === "completed" || d.status === "failed" || d.runtime_status === "Completed" || d.runtime_status === "Failed") return false;
      return 5_000;
    },
  });

  const resultsQuery = useQuery({
    queryKey: ["blast-results", jobId, subscriptionId, storageAccount, resourceGroup],
    queryFn: () => blastApi.listResults(jobId!, subscriptionId, storageAccount, resourceGroup),
    enabled: Boolean(jobId && subscriptionId && storageAccount),
    refetchInterval: (q) => {
      if (q.state.data?.files && q.state.data.files.length > 0) return false;
      if (q.state.data?.public_access_disabled) return false;
      return 30_000;
    },
  });

  const job = jobQuery.data;
  const allFiles = resultsQuery.data?.files ?? [];
  // Separate BLAST result files (.out) from debug/log artifacts
  const DEBUG_FILES = new Set(["blast-status.txt", "jobs.txt", "pods.txt"]);
  const resultFiles = allFiles.filter((f: BlastResultFile) => {
    const basename = f.name.split("/").pop() || "";
    return !DEBUG_FILES.has(basename) && !basename.endsWith(".log");
  });
  const debugFiles = allFiles.filter((f: BlastResultFile) => {
    const basename = f.name.split("/").pop() || "";
    return DEBUG_FILES.has(basename) || basename.endsWith(".log");
  });
  // Show result files first; if none exist, show debug files as fallback
  const files = resultFiles.length > 0 ? resultFiles : debugFiles;
  const hasOnlyDebugFiles = resultFiles.length === 0 && debugFiles.length > 0;
  const publicAccessDisabled = resultsQuery.data?.public_access_disabled === true;
  const customStatus = (typeof job?.custom_status === "object" && job?.custom_status !== null)
    ? (job.custom_status as Record<string, unknown>) : null;
  // Phase resolution: use multiple signals to determine the real status.
  // Priority: output.phase (orchestrator final result) > custom_status.phase > entity phase > runtime_status
  const outputPhase = (typeof job?.output === "object" && job?.output !== null)
    ? (job.output as Record<string, unknown>).phase as string | undefined : undefined;
  const outputStatus = (typeof job?.output === "object" && job?.output !== null)
    ? (job.output as Record<string, unknown>).status as string | undefined : undefined;
  const jobPhase = outputPhase || (customStatus?.phase as string) || job?.phase || job?.status;
  const isJobFailed = jobPhase === "failed" || jobPhase === "error" || jobPhase === "submit_failed" || jobPhase === "warmup_failed"
    || outputStatus === "failed";
  const phase = isJobFailed ? (jobPhase as string)
    : job?.runtime_status === "Completed" && !isJobFailed ? "completed"
    : job?.runtime_status === "Failed" ? "error"
    : jobPhase || "unknown";

  // Track phase transitions for toast notifications.
  // Only toast when we observe a LIVE transition (running → completed).
  // If the job is already terminal on first load, never toast.
  const initialPhaseRef = useRef<string | null>(null);
  useEffect(() => {
    if (prevPhaseRef.current === null) {
      // First render — record phase, never toast
      prevPhaseRef.current = phase;
      initialPhaseRef.current = phase;
      return;
    }
    // If the job was already terminal when we first loaded, skip all toasts
    const wasTerminalOnLoad = initialPhaseRef.current === "completed"
      || initialPhaseRef.current === "failed"
      || initialPhaseRef.current === "error"
      || initialPhaseRef.current === "submit_failed"
      || initialPhaseRef.current === "warmup_failed"
      || initialPhaseRef.current === "cancelled";
    if (wasTerminalOnLoad) {
      prevPhaseRef.current = phase;
      return;
    }
    if (phase && phase !== prevPhaseRef.current) {
      if (phase === "completed") toast("BLAST job completed successfully!", "success");
      else if (phase === "failed" || phase === "error") toast("BLAST job failed.", "error");
    }
    prevPhaseRef.current = phase;
  }, [phase, toast]);

  const handleDownload = async (file: BlastResultFile) => {
    if (!jobId) return;
    setDownloadingFile(file.name);
    try {
      const resp = await blastApi.downloadResult(jobId, subscriptionId, storageAccount, file.name);
      window.open(resp.download_url, "_blank");
    } catch (e) {
      toast(`Download failed: ${(e as Error).message}`, "error");
    } finally {
      setDownloadingFile(null);
    }
  };

  const cancelMutation = useMutation({
    mutationFn: () => blastApi.cancelJob(jobId!),
    onSuccess: () => {
      toast("Job cancelled.", "success");
      queryClient.invalidateQueries({ queryKey: ["blast-job", jobId] });
    },
    onError: (e) => toast(`Cancel failed: ${(e as Error).message}`, "error"),
  });

  const copyJobId = () => {
    if (jobId) {
      navigator.clipboard.writeText(jobId).catch(() => {});
      setCopiedId(true);
      setTimeout(() => setCopiedId(false), 2000);
    }
  };

  const isFailed = isJobFailed;
  const blastStatus = customStatus?.blast_status as string | undefined;
  const pollAttempt = customStatus?.poll_attempt as number | undefined;
  const runtimeStatus = job?.runtime_status as string | undefined;

  // Detect errors in submit/export logs
  const stepsObj = (customStatus?.steps ?? (typeof job?.output === "object" ? (job.output as Record<string, unknown>)?.steps : null) ?? {}) as Record<string, Record<string, unknown>>;
  const exportStep = stepsObj?.exporting_results as Record<string, unknown> | undefined;
  const submitStep = stepsObj?.submitting as Record<string, unknown> | undefined;
  const hasOutputFiles = exportStep?.has_output_files as boolean | undefined;
  const submitOutput = (submitStep?.output as string) ?? "";
  // Only match fatal errors — not non-fatal config warnings
  const submitHasFatalErrors = (() => {
    if (/ErrorCode:|<Error>/.test(submitOutput)) return true;
    // Check for ERROR: at line start, but exclude known non-fatal warnings
    const NON_FATAL = ["Unrecognized configuration parameter"];
    for (const line of submitOutput.split("\n")) {
      if (line.startsWith("ERROR:")) {
        const isFatal = !NON_FATAL.some((nf) => line.includes(nf));
        if (isFatal) return true;
      }
    }
    return false;
  })();
  // "completed" but submit had fatal errors or no .out files = treat as failed
  // However, trust the orchestrator's final verdict if it explicitly says "completed"
  const orchestratorSaysCompleted = outputStatus === "completed";
  const completedButFailed = phase === "completed" && !orchestratorSaysCompleted && (hasOutputFiles === false || submitHasFatalErrors);
  // Effective phase: override to submit_failed when completed-but-failed
  const effectivePhase = completedButFailed ? "submit_failed" : phase;
  const effectiveIsFailed = isFailed || completedButFailed;
  const effectiveColor = statusColor(effectivePhase === "submit_failed" ? "failed" : effectivePhase);
  const isRunning = !effectiveIsFailed && phase !== "completed" && phase !== "deleted" && phase !== "cancelled";

  return (
    <div className="page-stack">
      <header>
        <Link
          to="/blast/jobs"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "var(--space-2)",
            fontSize: 13,
            marginBottom: "var(--space-3)",
          }}
        >
          <ArrowLeft size={14} strokeWidth={1.5} /> All jobs
        </Link>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)" }}>
          <h1 style={{ margin: 0, flex: 1 }}>{job?.job_title || jobId}</h1>
          {job?.created_at && isRunning && (
            <span style={{ fontSize: 12, color: "var(--text-muted)", display: "inline-flex", alignItems: "center", gap: 4 }}>
              <Clock size={12} strokeWidth={1.5} />
              <ElapsedTimer startTime={job.created_at} />
            </span>
          )}
          {isRunning && (
            <button
              className="glass-button"
              onClick={() => setShowCancelConfirm(true)}
              disabled={cancelMutation.isPending}
              style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12, color: "var(--danger)" }}
            >
              <StopCircle size={14} strokeWidth={1.5} /> Cancel
            </button>
          )}
        </div>
      </header>

      {/* Job Info */}
      <section className="glass-card glass-card--strong">
        <h3 style={{ marginTop: 0 }}>Job Details</h3>
        {!job && (
          <div style={{ display: "flex", alignItems: "center", gap: 8 }} className="muted">
            <Loader2 size={14} className="spin" /> Loading job details...
          </div>
        )}
        {job && (
          <>
            {/* Live status banner */}
            {isRunning && (
              <div style={{
                padding: "12px 16px", marginBottom: "var(--space-3)", borderRadius: 10,
                background: "linear-gradient(135deg, rgba(110,159,255,0.08), rgba(110,159,255,0.03))",
                border: "1px solid rgba(110,159,255,0.18)",
                fontSize: 13, color: "var(--text-primary)",
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: blastStatus || job?.created_at ? 8 : 0 }}>
                  <Loader2 size={16} className="spin" style={{ color: "var(--accent)" }} />
                  <strong style={{ color: "var(--accent)", fontSize: 14 }}>
                    {PHASE_MESSAGES[phase] ?? `Phase: ${phase}`}
                  </strong>
                </div>
                {blastStatus && (
                  <div style={{ fontSize: 12, display: "flex", gap: 16, marginLeft: 26, color: "var(--text-muted)" }}>
                    <span>BLAST: <strong style={{ color: "var(--text-primary)" }}>{blastStatus}</strong></span>
                    {pollAttempt != null && <span>Poll #{pollAttempt} · ~{pollAttempt * 30}s elapsed</span>}
                  </div>
                )}
                {job?.created_at && (
                  <div style={{ fontSize: 11, marginLeft: 26, marginTop: 4, color: "var(--text-faint)" }}>
                    Started {new Date(job.created_at).toLocaleString()}
                    {runtimeStatus && <> · Orchestrator: {runtimeStatus}</>}
                    {" · Elapsed: "}<ElapsedTimer startTime={job.created_at} />
                  </div>
                )}
              </div>
            )}

            {/* Success banner — only when truly clean */}
            {phase === "completed" && !completedButFailed && !effectiveIsFailed && (
              <div style={{
                padding: "14px 16px", marginBottom: "var(--space-3)", borderRadius: 10,
                background: "linear-gradient(135deg, rgba(106,214,163,0.12), rgba(106,214,163,0.04))",
                border: "1px solid rgba(106,214,163,0.25)",
                display: "flex", alignItems: "center", gap: 12,
              }}>
                <div style={{
                  width: 36, height: 36, borderRadius: "50%", display: "flex",
                  alignItems: "center", justifyContent: "center",
                  background: "var(--success)", color: "#fff",
                  boxShadow: "0 0 16px rgba(106,214,163,0.4)",
                }}>
                  <Trophy size={18} />
                </div>
                <div>
                  <div style={{ fontSize: 15, fontWeight: 600, color: "var(--success)" }}>
                    Job Completed Successfully
                  </div>
                  <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
                    {resultFiles.length > 0 ? `${resultFiles.length} result file${resultFiles.length === 1 ? "" : "s"} ready for download` : hasOnlyDebugFiles ? "No BLAST results — diagnostic logs only" : "Checking results..."}
                  </div>
                </div>
              </div>
            )}


            {/* Failure banner */}
            {effectiveIsFailed && (() => {
              // Extract the most useful error info from the job data
              const errorOutput = completedButFailed ? submitOutput
                : (typeof job.output === "object" && job.output !== null)
                  ? (job.output as Record<string, unknown>).error as string ?? ""
                  : typeof job.error === "string" ? job.error : "";
              const failedStep = effectivePhase === "submit_failed" ? "Submit Job"
                : effectivePhase === "error" && customStatus?.phase === "running" ? "BLAST Run"
                : "Execution";
              // Find first actual error line from the output
              const errorLines = errorOutput.split("\n").filter(
                (l) => l.startsWith("ERROR:") || l.startsWith("✗") || l.includes("fatal") || l.includes("FATAL"),
              );
              const errorSummary = errorLines.length > 0 ? errorLines[0] : "";

              return (
                <div style={{
                  padding: "14px 16px", marginBottom: "var(--space-3)", borderRadius: 10,
                  background: "linear-gradient(135deg, rgba(224,123,138,0.12), rgba(224,123,138,0.04))",
                  border: "1px solid rgba(224,123,138,0.25)",
                }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    <div style={{
                      width: 36, height: 36, borderRadius: "50%", display: "flex",
                      alignItems: "center", justifyContent: "center",
                      background: "var(--danger)", color: "#fff", flexShrink: 0,
                    }}>
                      <XCircle size={18} />
                    </div>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontSize: 15, fontWeight: 600, color: "var(--danger)" }}>
                        Job Failed at {failedStep}
                      </div>
                      <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
                        {phase === "submit_failed" ? "The ElasticBLAST submit command failed. Check the execution steps below for details." : "An error occurred during execution. See the step logs for details."}
                      </div>
                    </div>
                  </div>
                  {errorSummary && (
                    <div style={{
                      marginTop: 10, padding: "8px 12px", borderRadius: 6,
                      background: "rgba(224,123,138,0.06)", border: "1px solid rgba(224,123,138,0.12)",
                      fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--danger)",
                      whiteSpace: "pre-wrap", wordBreak: "break-word",
                    }}>
                      {errorSummary}
                    </div>
                  )}
                </div>
              );
            })()}

            <div style={{
              display: "grid", gridTemplateColumns: "140px 1fr",
              gap: "var(--space-2) var(--space-4)", fontSize: 13,
            }}>
              <span className="muted">Job ID</span>
              <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <code className="code-val">{job.job_id}</code>
                <button className={`copy-btn${copiedId ? " copy-btn--copied" : ""}`} onClick={copyJobId} title="Copy Job ID">
                  {copiedId ? <CheckCircle2 size={12} /> : <Copy size={12} />}
                </button>
              </span>
              <span className="muted">Program</span>
              <span>{job.program}</span>
              <span className="muted">Database</span>
              <span style={{ wordBreak: "break-all" }}>{job.db}</span>
              <span className="muted">Status</span>
              <span style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
                <span style={{
                  width: 8, height: 8, borderRadius: 999,
                  background: effectiveColor, boxShadow: `0 0 8px ${effectiveColor}`,
                }} />
                {effectivePhase === "submit_failed" ? "failed" : effectivePhase}
              </span>
              <span className="muted">Created</span>
              <span>{job.created_at ? new Date(job.created_at).toLocaleString() : "—"}</span>
              <span className="muted">Duration</span>
              <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <Clock size={12} strokeWidth={1.5} style={{ color: "var(--text-faint)" }} />
                {job.created_at && isRunning
                  ? <ElapsedTimer startTime={job.created_at} />
                  : job.created_at && job.updated_at
                    ? (() => {
                        const ms = new Date(job.updated_at as string).getTime() - new Date(job.created_at).getTime();
                        const s = Math.floor(ms / 1000); const m = Math.floor(s / 60); const h = Math.floor(m / 60);
                        return h > 0 ? `${h}h ${m % 60}m ${s % 60}s` : m > 0 ? `${m}m ${s % 60}s` : `${s}s`;
                      })()
                    : "—"
                }
              </span>
              {job.config_snapshot && (
                <>
                  <span className="muted">E-value</span>
                  <span>{String((job.config_snapshot as Record<string, unknown>).evalue ?? "—")}</span>
                  <span className="muted">Max targets</span>
                  <span>{String((job.config_snapshot as Record<string, unknown>).max_target_seqs ?? "—")}</span>
                  <span className="muted">Machine</span>
                  <span>{String((job.config_snapshot as Record<string, unknown>).machine_type ?? "—")}</span>
                  <span className="muted">Nodes</span>
                  <span>{String((job.config_snapshot as Record<string, unknown>).num_nodes ?? "—")}</span>
                </>
              )}
              {/* Infrastructure info */}
              {job.infrastructure && (() => {
                const infra = job.infrastructure as Record<string, unknown>;
                return (<>
                  <span className="muted">Cluster</span>
                  <span><code style={{ fontSize: 11 }}>{String(infra.cluster_name ?? "—")}</code></span>
                  <span className="muted">Region</span>
                  <span>{String(infra.region ?? "—")}</span>
                  <span className="muted">Resource Group</span>
                  <span>{String(infra.resource_group ?? "—")}</span>
                  <span className="muted">Storage</span>
                  <span>{String(infra.storage_account ?? "—")}</span>
                </>);
              })()}
            </div>
          </>
        )}

        {/* Summary metric cards for completed jobs */}
        {job && phase === "completed" && !effectiveIsFailed && files.length > 0 && (
          <div className="metric-grid" style={{ marginTop: "var(--space-3)" }}>
            <div className="metric-block">
              <div className="mv">{files.length}</div>
              <div className="mu">Result files</div>
            </div>
            <div className="metric-block">
              <div className="mv">{formatBytes(files.reduce((sum, f) => sum + (f.size || 0), 0))}</div>
              <div className="mu">Total size</div>
            </div>
            <div className="metric-block">
              <div className="mv" style={{ color: completedButFailed ? "var(--danger)" : "var(--success)" }}>
                {completedButFailed
                  ? <><XCircle size={18} style={{ display: "inline", verticalAlign: "middle", marginRight: 4 }} />failed</>
                  : <><CheckCircle2 size={18} style={{ display: "inline", verticalAlign: "middle", marginRight: 4 }} />{phase}</>}
              </div>
              <div className="mu">Status</div>
            </div>
          </div>
        )}

        {job?.error && phase !== "failed" && phase !== "error" && (
          <div style={{
            marginTop: "var(--space-4)", padding: "var(--space-3)",
            background: "rgba(224, 123, 138, 0.12)", borderRadius: 8,
            color: "var(--danger)", fontSize: 13,
          }}>
            {String(job.error)}
          </div>
        )}
      </section>

      {/* Step Logs — GitHub Actions style collapsible */}
      {job && (
        <section className="glass-card" style={{ padding: "14px 16px" }}>
          <h3 style={{ margin: "0 0 10px 0", fontSize: 14, display: "flex", alignItems: "center", gap: 8 }}>
            <FileText size={15} strokeWidth={1.5} /> Execution Steps
          </h3>
          <StepLogSection phase={effectivePhase} job={job as unknown as Record<string, unknown>} subscriptionId={subscriptionId} storageAccount={storageAccount} resourceGroup={resourceGroup} />
        </section>
      )}

      {/* Results */}
      <section className="glass-card">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <h3 style={{ margin: 0, display: "flex", alignItems: "center", gap: 8 }}>
            <FileText size={16} strokeWidth={1.5} /> Results
          </h3>
          <button
            className="glass-button"
            onClick={() => resultsQuery.refetch()}
            disabled={resultsQuery.isFetching}
            style={{ display: "flex", alignItems: "center", gap: 4 }}
          >
            <RefreshCw size={14} strokeWidth={1.5} className={resultsQuery.isFetching ? "spin" : ""} /> Refresh
          </button>
        </div>

        {isRunning ? (
          <div style={{
            marginTop: "var(--space-3)", padding: "16px", borderRadius: 10,
            background: "var(--bg-tertiary)", fontSize: 13,
            display: "flex", alignItems: "center", gap: 10,
          }}>
            <Loader2 size={16} className="spin" style={{ color: "var(--accent)", flexShrink: 0 }} />
            <span style={{ color: "var(--text-muted)" }}>
              Results will appear here once the job completes. Current phase:{" "}
              <strong style={{ color: "var(--accent)" }}>{effectivePhase}</strong>
            </span>
          </div>
        ) : effectiveIsFailed ? (
          <div style={{
            marginTop: "var(--space-3)", padding: "16px", borderRadius: 10,
            background: "rgba(224,123,138,0.04)", border: "1px solid rgba(224,123,138,0.15)",
            fontSize: 13, display: "flex", alignItems: "center", gap: 10,
          }}>
            <XCircle size={16} style={{ color: "var(--danger)", flexShrink: 0 }} />
            <span style={{ color: "var(--text-muted)" }}>
              No results available — the job failed at the <strong style={{ color: "var(--danger)" }}>{effectivePhase === "submit_failed" ? "Submit" : "Execution"}</strong> step.
            </span>
          </div>
        ) : !subscriptionId || !storageAccount ? (
          <div className="muted" style={{ marginTop: "var(--space-3)" }}>
            <p>Cannot load results — missing Azure configuration.</p>
            <Link to="/" className="glass-button glass-button--primary" style={{ textDecoration: "none", fontSize: 12, marginTop: 8, display: "inline-flex" }}>
              Configure on Dashboard →
            </Link>
          </div>
        ) : publicAccessDisabled || resultsQuery.isError ? (
          <StorageLockedPanel
            subscriptionId={subscriptionId}
            storageAccount={storageAccount}
            resourceGroup={resourceGroup}
            jobId={jobId!}
            onUnlocked={() => {
              queryClient.invalidateQueries({ queryKey: ["blast-results"] });
              resultsQuery.refetch();
            }}
          />
        ) : files.length === 0 ? (
          <div style={{ marginTop: "var(--space-3)" }}>
            {phase === "completed" ? (
              <div style={{
                padding: "16px", borderRadius: 10,
                background: "var(--bg-tertiary)",
              }}>
                <div style={{ fontSize: 13, color: "var(--text-primary)", marginBottom: 10 }}>
                  No BLAST result files (.out) found in <code style={{ fontSize: 12 }}>results/{jobId}/</code>.
                </div>
                <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.6 }}>
                  This typically means the BLAST search returned no hits for the given query and database combination.
                </div>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  <button className="glass-button" onClick={() => resultsQuery.refetch()} style={{ fontSize: 12 }}>
                    <RefreshCw size={13} /> Try Again
                  </button>
                  <Link to="/terminal" className="glass-button" style={{ textDecoration: "none", fontSize: 12 }}>
                    <Server size={13} /> Check Terminal
                  </Link>
                  <Link
                    to={`/blast/submit?resubmit=${encodeURIComponent(jobId!)}`}
                    className="glass-button glass-button--primary"
                    style={{ textDecoration: "none", fontSize: 12 }}
                  >
                    <Send size={13} /> Re-submit with Same Parameters
                  </Link>
                </div>
                {/* Results location */}
                <div style={{
                  marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--border-weak)",
                  display: "grid", gridTemplateColumns: "80px 1fr", gap: "3px 10px", fontSize: 11, color: "var(--text-faint)",
                }}>
                  <span>Account</span><code style={{ fontSize: 11 }}>{storageAccount}</code>
                  <span>Container</span><code style={{ fontSize: 11 }}>results</code>
                  <span>Prefix</span><code style={{ fontSize: 11 }}>{jobId}/</code>
                </div>
              </div>
            ) : (
              <p className="muted">Results will appear here once the job completes.</p>
            )}
          </div>
        ) : (
          <div style={{ marginTop: "var(--space-3)" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--glass-border)" }}>
                  <th style={{ textAlign: "left", padding: "8px 12px", color: "var(--text-muted)", fontWeight: 500, fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em" }}>
                    File
                  </th>
                  <th style={{ textAlign: "right", padding: "8px 12px", color: "var(--text-muted)", fontWeight: 500, fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em" }}>
                    Size
                  </th>
                  <th style={{ textAlign: "right", padding: "8px 12px", color: "var(--text-muted)", fontWeight: 500, fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em" }}>
                    Modified
                  </th>
                  <th style={{ width: 60 }} />
                </tr>
              </thead>
              <tbody>
                {files.map((f) => {
                  const fname = f.name.split("/").pop() || f.name;
                  const ext = fname.split(".").pop()?.toLowerCase() || "";
                  const isResult = ext === "out" || ext === "gz" || ext === "asn";
                  const isLog = ext === "log";
                  const typeColor = isResult ? "var(--success)" : isLog ? "var(--warning)" : "var(--text-faint)";
                  const typeLabel = isResult ? "RESULT" : isLog ? "LOG" : "INFO";
                  return (
                  <tr key={f.name} style={{ borderBottom: "1px solid var(--glass-border)" }}>
                    <td style={{ padding: "8px 12px", display: "flex", alignItems: "center", gap: 8 }}>
                      <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 3, background: `color-mix(in srgb, ${typeColor} 15%, transparent)`, color: typeColor, fontWeight: 600, letterSpacing: "0.04em", flexShrink: 0 }}>
                        {typeLabel}
                      </span>
                      <code style={{ fontSize: 12 }}>{fname}</code>
                    </td>
                    <td style={{ padding: "8px 12px", textAlign: "right" }} className="muted">
                      {f.size != null ? formatBytes(f.size) : "—"}
                    </td>
                    <td style={{ padding: "8px 12px", textAlign: "right" }} className="muted">
                      {f.last_modified ? new Date(f.last_modified).toLocaleString() : "—"}
                    </td>
                    <td style={{ padding: "8px 12px", textAlign: "right" }}>
                      <button
                        className="glass-button"
                        onClick={() => handleDownload(f)}
                        disabled={downloadingFile === f.name}
                        title="Download"
                      >
                        {downloadingFile === f.name ? <Loader2 size={14} className="spin" /> : <Download size={14} strokeWidth={1.5} />}
                      </button>
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
            {hasOnlyDebugFiles && (
              <div style={{ marginTop: 12, padding: "10px 14px", borderRadius: 8, background: "rgba(240,198,116,0.08)", fontSize: 12, color: "var(--text-muted)" }}>
                <AlertTriangle size={13} style={{ verticalAlign: "middle", marginRight: 6, color: "var(--warning)" }} />
                No BLAST result files (.out) were produced. The files above are diagnostic logs from the cluster.
                This typically means the search returned no hits for the query/database combination.
              </div>
            )}
            {debugFiles.length > 0 && resultFiles.length > 0 && (
              <details style={{ marginTop: 14, fontSize: 12 }}>
                <summary style={{ cursor: "pointer", color: "var(--text-muted)" }}>
                  {debugFiles.length} diagnostic file{debugFiles.length > 1 ? "s" : ""} (logs, status)
                </summary>
                <div style={{ marginTop: 8, display: "flex", flexWrap: "wrap", gap: 8 }}>
                  {debugFiles.map((f) => {
                    const fname = f.name.split("/").pop() || f.name;
                    return (
                      <button key={f.name} className="glass-button" style={{ fontSize: 11, padding: "4px 10px" }}
                        onClick={() => handleDownload(f)}>
                        <Download size={11} /> {fname}
                      </button>
                    );
                  })}
                </div>
              </details>
            )}
          </div>
        )}
      </section>

      {/* Cancel confirmation dialog */}
      <ConfirmDialog
        open={showCancelConfirm}
        title="Cancel BLAST Job"
        message={`Are you sure you want to cancel "${job?.job_title || jobId}"? This will terminate the running orchestrator. Any in-progress work on the AKS cluster may need manual cleanup.`}
        confirmLabel="Cancel Job"
        onConfirm={() => {
          setShowCancelConfirm(false);
          cancelMutation.mutate();
        }}
        onCancel={() => setShowCancelConfirm(false)}
      />
    </div>
  );
}
