import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Clipboard,
  Copy,
  Database,
  Dna,
  FileSearch,
  Gauge,
  KeyRound,
  LifeBuoy,
  RefreshCw,
  SendToBack,
  ShieldCheck,
  Sparkles,
} from "lucide-react";

import { fetchApiRaw } from "@/api/client";
import {
  classifyCommand,
  COCKPIT_CHAPTERS,
  COCKPIT_WORKFLOWS,
  INNOVATION_CAPABILITIES,
  normaliseCommandForTerminalInsert,
  type CommandImpact,
  type CommandRisk,
} from "@/pages/terminal/terminalCockpitModel";
import {
  analyzeDiagnosticReadiness,
  analyzeDiagnosticTriage,
  buildDiagnosticRunbookDraft,
  DEFAULT_DIAGNOSTIC_CONTEXT,
  DIAGNOSTIC_WORKFLOWS,
  evaluateDiagnosticHardeningReview,
  evaluateDiagnosticMaturity,
  getDiagnosticWorkflow,
  triageBlastOutfmt6,
  type ControlRole,
  type DiagnosticGuardLevel,
  type DiagnosticInputType,
  type DiagnosticSampleContext,
} from "@/pages/terminal/terminalDiagnosticModel";

interface TerminalCockpitProps {
  connectionStatus: "connecting" | "connected" | "disconnected" | "error";
  callerDisplay: string | null;
  shellUser: string | null;
  onCopyCommand: (command: string) => void;
  onInsertCommand: (command: string) => void;
}

interface TerminalHealthResponse {
  status: "ok" | "degraded" | "down";
  upstream_status?: number;
  error?: string;
}

// T2: result of GET /api/terminal/azure-cli.
interface AzureCliStatus {
  status: "signed_in" | "signed_out" | "unknown";
  user?: string | null;
  tenant_id?: string | null;
  subscription_id?: string | null;
  hint?: string | null;
  error?: string | null;
  checked_at?: number;
  cached?: boolean;
  cache_age_s?: number;
}

async function fetchTerminalHealth(): Promise<TerminalHealthResponse> {
  const response = await fetchApiRaw("/terminal/health", { method: "GET" });
  if (!response.ok) return { status: "down", error: `HTTP ${response.status}` };
  return (await response.json()) as TerminalHealthResponse;
}

async function fetchAzureCliStatus(force = false): Promise<AzureCliStatus> {
  const path = force ? "/terminal/azure-cli?force=true" : "/terminal/azure-cli";
  const response = await fetchApiRaw(path, { method: "GET" });
  if (!response.ok) {
    return { status: "unknown", error: `HTTP ${response.status}` };
  }
  return (await response.json()) as AzureCliStatus;
}

function impactLabel(impact: CommandImpact): string {
  return impact.replace("-", " ");
}

function riskTone(risk: CommandRisk): string {
  if (risk === "high") return "danger";
  if (risk === "medium") return "warning";
  return "success";
}

function guardTone(level: DiagnosticGuardLevel): string {
  if (level === "critical") return "danger";
  if (level === "warning") return "warning";
  return "success";
}

export function TerminalCockpit({
  connectionStatus,
  callerDisplay,
  shellUser,
  onCopyCommand,
  onInsertCommand,
}: TerminalCockpitProps) {
  const [command, setCommand] = useState("az account show -o table");
  const [diagnosticContext, setDiagnosticContext] = useState<DiagnosticSampleContext>(
    DEFAULT_DIAGNOSTIC_CONTEXT,
  );
  const [blastTsv, setBlastTsv] = useState("");
  const analysis = useMemo(() => classifyCommand(command), [command]);
  const diagnosticWorkflow = getDiagnosticWorkflow(diagnosticContext.workflowId);
  const diagnosticGuards = useMemo(
    () => analyzeDiagnosticReadiness(command, diagnosticContext),
    [command, diagnosticContext],
  );
  const blastTriage = useMemo(
    () => triageBlastOutfmt6(blastTsv, diagnosticContext.workflowId),
    [blastTsv, diagnosticContext.workflowId],
  );
  const triageGuards = useMemo(
    () => analyzeDiagnosticTriage(diagnosticContext, blastTriage),
    [blastTriage, diagnosticContext],
  );
  const hardeningReview = useMemo(
    () => evaluateDiagnosticHardeningReview(command, diagnosticContext, blastTriage),
    [blastTriage, command, diagnosticContext],
  );
  const maturity = useMemo(
    () => evaluateDiagnosticMaturity(command, diagnosticContext, blastTriage),
    [blastTriage, command, diagnosticContext],
  );
  const hardeningCounts = useMemo(() => {
    const failed = hardeningReview.filter((item) => !item.passed);
    let blockers = 0;
    let warnings = 0;
    for (const item of failed) {
      if (item.severity === "blocker") blockers += 1;
      if (item.severity === "warning") warnings += 1;
    }
    return { failed, blockers, warnings };
  }, [hardeningReview]);
  const failedHardeningReview = hardeningCounts.failed;
  const blockerCount = hardeningCounts.blockers;
  const warningCount = hardeningCounts.warnings;
  const passedHardeningCount = hardeningReview.length - failedHardeningReview.length;
  const cockpitGuards = useMemo(
    () => [
      ...diagnosticGuards,
      ...triageGuards,
      ...failedHardeningReview
        .filter((item) => item.severity !== "advisory")
        .slice(0, 8)
        .map((item) => ({
          level: item.severity === "blocker" ? "critical" as const : "warning" as const,
          message: item.rationale,
        })),
    ],
    [diagnosticGuards, failedHardeningReview, triageGuards],
  );
  const runbookDraft = useMemo(
    () => buildDiagnosticRunbookDraft(diagnosticContext, blastTriage, cockpitGuards),
    [blastTriage, cockpitGuards, diagnosticContext],
  );
  const health = useQuery({
    queryKey: ["terminal-sidecar-health"],
    queryFn: fetchTerminalHealth,
    refetchInterval: 30_000,
    staleTime: 15_000,
    retry: false,
  });
  // T2: poll the Azure CLI sign-in status separately from sidecar health.
  // Backend caches the underlying `az account show` call for 60 s so this is
  // safe at a 2-minute UI refresh.
  const azureCli = useQuery({
    queryKey: ["terminal-azure-cli"],
    queryFn: () => fetchAzureCliStatus(false),
    refetchInterval: 120_000,
    staleTime: 60_000,
    retry: false,
  });

  const healthStatus = health.data?.status ?? (health.isLoading ? "checking" : "unknown");
  const liveCount = INNOVATION_CAPABILITIES.filter((item) => item.status === "live").length;
  const guardedCount = INNOVATION_CAPABILITIES.filter((item) => item.status === "guarded").length;
  const foundationCount = INNOVATION_CAPABILITIES.filter((item) => item.status === "foundation").length;
  const insertCommand = normaliseCommandForTerminalInsert(command);
  const terminalReady = connectionStatus === "connected";
  const canInsert = terminalReady && insertCommand.length > 0 && analysis.risk !== "high";
  const insertTitle = !terminalReady
    ? "Terminal must be connected before running commands"
    : insertCommand.length === 0
      ? "Type a command before running it"
      : analysis.risk === "high"
        ? "High-risk commands must be copied and reviewed manually"
        : "Insert and run the reviewed command";

  const updateDiagnosticContext = <K extends keyof DiagnosticSampleContext>(
    key: K,
    value: DiagnosticSampleContext[K],
  ) => {
    setDiagnosticContext((current) => ({ ...current, [key]: value }));
  };

  return (
    <aside className="terminal-cockpit" aria-label="Terminal cockpit">
      <div className="terminal-cockpit__header">
        <div className="terminal-cockpit__title">
          <Sparkles size={15} strokeWidth={1.5} />
          Terminal Cockpit
        </div>
        <div className="terminal-cockpit__subtitle">intent, safety, workflow, and recovery</div>
      </div>

      <section className="terminal-cockpit__health" aria-label="Terminal health">
        <div className="terminal-cockpit__health-item">
          <Activity size={13} strokeWidth={1.5} />
          <span>Socket</span>
          <strong data-state={connectionStatus}>{connectionStatus}</strong>
        </div>
        <div className="terminal-cockpit__health-item">
          <ShieldCheck size={13} strokeWidth={1.5} />
          <span>Sidecar</span>
          <strong data-state={healthStatus}>{healthStatus}</strong>
        </div>
        <div className="terminal-cockpit__health-item">
          <Gauge size={13} strokeWidth={1.5} />
          <span>Shell</span>
          <strong>{shellUser ?? "pending"}</strong>
        </div>
        {/* T2: Azure CLI sign-in indicator. Shows last-known status and lets
            the user trigger a fresh probe (bypasses the 60 s cache). */}
        <div
          className="terminal-cockpit__health-item"
          title={
            azureCli.data?.status === "signed_in" && azureCli.data?.user
              ? `Signed in as ${azureCli.data.user}`
              : azureCli.data?.hint ??
                azureCli.data?.error ??
                "Run `az login --use-device-code` inside the terminal to sign in."
          }
        >
          <KeyRound size={13} strokeWidth={1.5} />
          <span>az login</span>
          <strong data-state={azureCli.data?.status ?? (azureCli.isLoading ? "checking" : "unknown")}>
            {azureCli.isLoading
              ? "checking"
              : azureCli.data?.status === "signed_in"
                ? "signed in"
                : azureCli.data?.status === "signed_out"
                  ? "signed out"
                  : "unknown"}
          </strong>
          <button
            type="button"
            onClick={() => azureCli.refetch()}
            disabled={azureCli.isFetching}
            aria-label="Re-check az login status"
            title="Re-check az login status (bypasses 60s cache)"
            style={{
              background: "none",
              border: "none",
              padding: 0,
              marginLeft: 6,
              cursor: azureCli.isFetching ? "wait" : "pointer",
              color: "var(--text-faint)",
              display: "inline-flex",
              alignItems: "center",
            }}
          >
            <RefreshCw size={11} strokeWidth={1.5} className={azureCli.isFetching ? "spin" : undefined} />
          </button>
        </div>
      </section>

      <section className="terminal-cockpit__panel terminal-cockpit__panel--intent">
        <div className="terminal-cockpit__panel-title">
          <LifeBuoy size={14} strokeWidth={1.5} />
          Command Preview
        </div>
        <textarea
          className="terminal-cockpit__command-input"
          value={command}
          spellCheck={false}
          onChange={(event) => setCommand(event.target.value)}
          aria-label="Command to preview"
        />
        <div className="terminal-cockpit__chips">
          <span className={`terminal-cockpit__chip terminal-cockpit__chip--${riskTone(analysis.risk)}`}>
            {analysis.risk} risk
          </span>
          <span className="terminal-cockpit__chip">{impactLabel(analysis.impact)}</span>
          <span className="terminal-cockpit__chip">{callerDisplay ?? "local identity"}</span>
        </div>
        <p>{analysis.summary}</p>
        <ul className="terminal-cockpit__checks">
          {analysis.checks.map((check) => (
            <li key={check}>{check}</li>
          ))}
        </ul>
        {analysis.rollback && <div className="terminal-cockpit__rollback">{analysis.rollback}</div>}
        <div className="terminal-cockpit__actions">
          <button
            type="button"
            className="glass-button"
            onClick={() => onInsertCommand(insertCommand)}
            disabled={!canInsert}
            title={insertTitle}
          >
            <SendToBack size={13} strokeWidth={1.5} />
            Insert
          </button>
          <button type="button" className="glass-button" onClick={() => onCopyCommand(command)}>
            <Copy size={13} strokeWidth={1.5} />
            Copy
          </button>
          {analysis.saferCommand && (
            <button
              type="button"
              className="glass-button"
              onClick={() => setCommand(analysis.saferCommand ?? command)}
            >
              Safer
            </button>
          )}
        </div>
      </section>

      <section className="terminal-cockpit__panel terminal-cockpit__panel--diagnostic">
        <div className="terminal-cockpit__panel-title">
          <Dna size={14} strokeWidth={1.5} />
          Diagnostic Context
        </div>
        <div className="terminal-cockpit__preset-grid" aria-label="Diagnostic workflow presets">
          {DIAGNOSTIC_WORKFLOWS.map((workflow) => (
            <button
              type="button"
              className="terminal-cockpit__preset"
              data-active={workflow.id === diagnosticContext.workflowId}
              key={workflow.id}
              onClick={() => {
                setDiagnosticContext((current) => ({
                  ...current,
                  workflowId: workflow.id,
                  inputType: workflow.preferredInputs[0],
                  database: workflow.preferredDatabases[0],
                }));
                setCommand(workflow.recommendedCommands[0]);
              }}
            >
              {workflow.label}
            </button>
          ))}
        </div>
        <p>{diagnosticWorkflow.summary}</p>
        <div className="terminal-cockpit__form-grid">
          <label>
            <span>Sample ID</span>
            <input
              value={diagnosticContext.sampleId}
              onChange={(event) => updateDiagnosticContext("sampleId", event.target.value)}
              placeholder="S-001"
            />
          </label>
          <label>
            <span>Input</span>
            <select
              value={diagnosticContext.inputType}
              onChange={(event) => updateDiagnosticContext("inputType", event.target.value as DiagnosticInputType)}
            >
              <option value="fasta">FASTA</option>
              <option value="fastq">FASTQ</option>
              <option value="contigs">Contigs</option>
              <option value="primers">Primers</option>
              <option value="blast-tabular">BLAST TSV</option>
            </select>
          </label>
          <label>
            <span>Control</span>
            <select
              value={diagnosticContext.controlRole}
              onChange={(event) => updateDiagnosticContext("controlRole", event.target.value as ControlRole)}
            >
              <option value="sample">Sample</option>
              <option value="positive-control">Positive control</option>
              <option value="negative-control">Negative control</option>
              <option value="ntc">NTC</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>
            <span>Database</span>
            <input
              value={diagnosticContext.database}
              onChange={(event) => updateDiagnosticContext("database", event.target.value)}
              placeholder="nt release 2026-05"
            />
          </label>
          <label className="terminal-cockpit__form-wide">
            <span>Organism group</span>
            <input
              value={diagnosticContext.organismGroup}
              onChange={(event) => updateDiagnosticContext("organismGroup", event.target.value)}
              placeholder="Respiratory bacteria"
            />
          </label>
        </div>
        <div className="terminal-cockpit__recommendations">
          {diagnosticWorkflow.recommendedCommands.map((item) => (
            <button type="button" key={item} onClick={() => setCommand(item)}>
              <code>{item}</code>
            </button>
          ))}
        </div>
      </section>

      <section className="terminal-cockpit__panel">
        <div className="terminal-cockpit__panel-title">
          <ShieldCheck size={14} strokeWidth={1.5} />
          Diagnostic Guards
        </div>
        <div className="terminal-cockpit__guard-list">
          {cockpitGuards.length ? (
            cockpitGuards.map((guard) => (
              <div className={`terminal-cockpit__guard terminal-cockpit__guard--${guardTone(guard.level)}`} key={guard.message}>
                <span>{guard.level}</span>
                <small>{guard.message}</small>
              </div>
            ))
          ) : (
            <div className="terminal-cockpit__guard terminal-cockpit__guard--success">
              <span>ready</span>
              <small>No diagnostic guard warnings for the current command and context.</small>
            </div>
          )}
        </div>
        <div className="terminal-cockpit__mini-checks">
          <div>
            <strong>QC</strong>
            {diagnosticWorkflow.qualityChecks.map((check) => <small key={check}>{check}</small>)}
          </div>
          <div>
            <strong>Interpretation</strong>
            {diagnosticWorkflow.interpretationChecks.map((check) => <small key={check}>{check}</small>)}
          </div>
        </div>
      </section>

      <section className="terminal-cockpit__panel terminal-cockpit__panel--maturity">
        <div className="terminal-cockpit__panel-title">
          <Gauge size={14} strokeWidth={1.5} />
          Maturity Ladder
          <span className="terminal-cockpit__coverage-count">
            Level {maturity.currentLevel}/10
          </span>
        </div>
        <div className="terminal-cockpit__maturity-hero">
          <strong>{maturity.nextLevel ? `Next: Level ${maturity.nextLevel.level}` : "Level 10 reached"}</strong>
          <span>{maturity.nextLevel?.label ?? "Audit-Ready Evidence Package"}</span>
          <small>{maturity.nextLevel?.critique ?? "No open level criteria remain for the current evidence package."}</small>
        </div>
        {maturity.nextActions.length > 0 && (
          <div className="terminal-cockpit__maturity-actions">
            {maturity.nextActions.map((action) => (
              <span key={action}>{action}</span>
            ))}
          </div>
        )}
        <div className="terminal-cockpit__maturity-list" aria-label="Diagnostic maturity levels">
          {maturity.levels.map((level) => (
            <div className="terminal-cockpit__maturity-level" data-state={level.status} key={level.level}>
              <span>{level.level}</span>
              <div>
                <strong>{level.label}</strong>
                <small>{level.objective}</small>
                {level.status === "current" && level.openCriteria.length > 0 && (
                  <em>{level.openCriteria.join(" · ")}</em>
                )}
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="terminal-cockpit__panel terminal-cockpit__panel--review">
        <div className="terminal-cockpit__panel-title">
          <FileSearch size={14} strokeWidth={1.5} />
          Cold Review
          <span className="terminal-cockpit__coverage-count">
            {passedHardeningCount}/{hardeningReview.length} passed
          </span>
        </div>
        <div className="terminal-cockpit__review-summary">
          <span data-state={blockerCount > 0 ? "danger" : "success"}>{blockerCount} blockers</span>
          <span data-state={warningCount > 0 ? "warning" : "success"}>{warningCount} warnings</span>
          <span>{failedHardeningReview.length} open checks</span>
        </div>
        <div className="terminal-cockpit__review-list">
          {failedHardeningReview.map((item) => (
            <div className="terminal-cockpit__review-item" data-severity={item.severity} key={item.id}>
              <span>{item.category}</span>
              <strong>{item.label}</strong>
              <small>{item.rationale}</small>
            </div>
          ))}
          {failedHardeningReview.length === 0 && (
            <div className="terminal-cockpit__review-item" data-severity="advisory">
              <span>ready</span>
              <strong>No open hardening checks</strong>
              <small>The current command, context, and triage passed the Cockpit review checklist.</small>
            </div>
          )}
        </div>
      </section>

      <section className="terminal-cockpit__panel terminal-cockpit__panel--triage">
        <div className="terminal-cockpit__panel-title">
          <FileSearch size={14} strokeWidth={1.5} />
          BLAST Result Triage
        </div>
        <textarea
          className="terminal-cockpit__command-input terminal-cockpit__triage-input"
          value={blastTsv}
          spellCheck={false}
          onChange={(event) => setBlastTsv(event.target.value)}
          placeholder="Paste BLAST outfmt 6 TSV with qcovs here"
          aria-label="BLAST outfmt 6 TSV"
        />
        <div className="terminal-cockpit__triage-summary">
          <span>{blastTriage.hitCount} hits</span>
          <span>Evidence: {blastTriage.evidenceLevel}</span>
          {blastTriage.topHit && <span>Top: {blastTriage.topHit.subjectId}</span>}
        </div>
        {blastTriage.topHit && (
          <div className="terminal-cockpit__top-hit">
            <Database size={13} strokeWidth={1.5} />
            <span>{blastTriage.topHit.identity}% identity</span>
            <span>{blastTriage.topHit.queryCoverage ?? "unknown"}% qcovs</span>
            <span>bitscore {blastTriage.topHit.bitScore}</span>
          </div>
        )}
        <ul className="terminal-cockpit__checks">
          {blastTriage.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
        <button type="button" className="glass-button" onClick={() => onCopyCommand(runbookDraft)}>
          <Copy size={13} strokeWidth={1.5} />
          Copy Evidence Summary
        </button>
      </section>

      <section className="terminal-cockpit__panel">
        <div className="terminal-cockpit__panel-title">
          <Clipboard size={14} strokeWidth={1.5} />
          Workflow Palette
        </div>
        <div className="terminal-cockpit__workflow-list">
          {COCKPIT_WORKFLOWS.map((workflow) => (
            <button
              type="button"
              className="terminal-cockpit__workflow"
              key={workflow.id}
              onClick={() => setCommand(workflow.command)}
              title={workflow.intent}
            >
              <span>{workflow.label}</span>
              <code>{workflow.command}</code>
            </button>
          ))}
        </div>
      </section>

      <section className="terminal-cockpit__panel">
        <div className="terminal-cockpit__panel-title">Session Chapters</div>
        <div className="terminal-cockpit__chapters">
          {COCKPIT_CHAPTERS.map((chapter) => (
            <div className="terminal-cockpit__chapter" data-state={chapter.status} key={chapter.id}>
              <span>{chapter.label}</span>
              <small>{chapter.detail}</small>
            </div>
          ))}
        </div>
      </section>

      <section className="terminal-cockpit__panel">
        <div className="terminal-cockpit__panel-title">
          Innovation Coverage
          <span className="terminal-cockpit__coverage-count">
            {liveCount} live · {guardedCount} guarded · {foundationCount} foundation
          </span>
        </div>
        <div className="terminal-cockpit__capabilities">
          {INNOVATION_CAPABILITIES.map((item) => {
            const Icon = item.icon;
            return (
              <div className="terminal-cockpit__capability" data-state={item.status} key={item.id}>
                <Icon size={13} strokeWidth={1.5} />
                <span>{item.label}</span>
                <small>{item.status}</small>
              </div>
            );
          })}
        </div>
      </section>
    </aside>
  );
}
