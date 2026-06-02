import { useEffect, useRef, useState } from "react";
import { Save, CheckCircle2, Loader2, Play, ShieldAlert } from "lucide-react";
import { Link } from "react-router-dom";

import { BLASTN_OPTIMIZE, type FormState } from "@/pages/blastSubmitModel";
import { BlastCommandPreview } from "@/pages/blastSubmit/ui";
import {
  runtimeShardingDisplay,
  runtimeWarmupDisplay,
} from "@/pages/blastSubmit/runtimeSummaryDisplay";
import type { PreFlightResult } from "@/pages/blastSubmit/usePreFlight";
import { PreFlightResultPanel } from "@/pages/blastSubmit/PreFlightResultPanel";
import type { ProgramMeta, ToastFn } from "@/pages/blastSubmit/types";
import type { MissingItem } from "@/pages/blastSubmit/submitValidation";

/* ─── Helpers ──────────────────────────────────────────────────────── */

function formatSavedAgo(when: Date | null | undefined, now: number): string {
  if (!when) return "Not saved yet";
  const seconds = Math.max(0, Math.round((now - when.getTime()) / 1000));
  if (seconds < 5) return "Saved just now";
  if (seconds < 60) return `Saved ${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `Saved ${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  return `Saved ${hours}h ago`;
}

/* ─── Props ────────────────────────────────────────────────────────── */

export interface SubmitSummaryRailProps {
  form: FormState;
  programMeta: ProgramMeta;
  toast: ToastFn;
  readySteps: { ok: boolean; label: string }[];
  readyCount: number;
  missing: MissingItem[];
  searchSummary: string;
  paramsSummary: string;
  canSubmit: boolean;
  submitPending: boolean;
  preFlightResult: PreFlightResult | null;
  preFlightPending: boolean;
  effectiveSearchSpace?: number;
  lastSavedAt?: Date | null;
  /**
   * The sharding mode that will actually be submitted. The submit payload
   * derives this from cluster + warm + capacity state, independently of the
   * reconcile effect that mutates ``form.sharding_mode``. Displaying it (rather
   * than the raw ``form.sharding_mode``) keeps the Runtime summary truthful even
   * before the reconcile effect has run, e.g. while warmup-status is still
   * resolving. Defaults to ``form.sharding_mode`` when omitted.
   */
  effectiveShardingMode?: FormState["sharding_mode"];
  /**
   * Whether the selected database is already warm on the selected cluster.
   * When true, warmup is effectively satisfied even if ``form.enable_warmup``
   * has not yet been flipped on by the reconcile effect.
   */
  isDbAlreadyWarm?: boolean;
  /** When set, overrides the submit-button title with the
   *  "you do not have permission to submit BLAST jobs" tooltip
   *  computed by ``permissionDeniedTooltip``. Critique #6. */
  permissionTooltip?: string;
  set: <K extends keyof FormState>(key: K, value: FormState[K]) => void;
  onPreFlight: () => void;
  onSubmit: () => void;
  now: number;
}

/* ─── Component ────────────────────────────────────────────────────── */

export function SubmitSummaryRail({
  form,
  programMeta,
  toast,
  readySteps,
  readyCount,
  missing,
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  searchSummary: _searchSummary,
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  paramsSummary: _paramsSummary,
  canSubmit,
  submitPending,
  preFlightResult,
  preFlightPending,
  effectiveSearchSpace,
  lastSavedAt,
  effectiveShardingMode,
  isDbAlreadyWarm,
  permissionTooltip,
  set,
  onPreFlight,
  onSubmit,
  now,
}: SubmitSummaryRailProps) {
  const preFlightBlocked = preFlightResult != null && preFlightResult.ready === false;
  const runDisabled = !canSubmit || preFlightBlocked || submitPending;
  // Critique #6: ``permissionTooltip`` wins so the user sees WHY
  // submission is blocked when they lack ``can_submit_blast``.
  const runTitle = permissionTooltip
    ? permissionTooltip
    : preFlightBlocked
      ? `Resolve ${preFlightResult?.critical_blockers ?? 0} pre-flight blocker(s) before submitting`
      : !canSubmit
        ? "Fill in the required fields above"
        : undefined;

  // Focus + pulse the Run BLAST button when validation transitions to ready,
  // but never steal focus from an active text input the user is still typing in.
  const runBtnRef = useRef<HTMLButtonElement | null>(null);
  const wasReadyRef = useRef(false);
  const [readyPulse, setReadyPulse] = useState(false);
  useEffect(() => {
    const ready = canSubmit && !preFlightBlocked && !submitPending;
    const wasReady = wasReadyRef.current;
    wasReadyRef.current = ready;
    if (!ready || wasReady) return;
    const ae = document.activeElement as HTMLElement | null;
    const tag = ae?.tagName;
    const inTextField =
      tag === "INPUT" ||
      tag === "TEXTAREA" ||
      tag === "SELECT" ||
      (ae?.isContentEditable ?? false);
    if (!inTextField && runBtnRef.current && runBtnRef.current.offsetParent !== null) {
      runBtnRef.current.focus({ preventScroll: false });
    }
    setReadyPulse(true);
    const t = window.setTimeout(() => setReadyPulse(false), 1500);
    return () => window.clearTimeout(t);
  }, [canSubmit, preFlightBlocked, submitPending]);

  const dbName = form.db ? form.db.split("/").pop() || form.db : "—";
  const optimizeLabel =
    form.program === "blastn"
      ? (BLASTN_OPTIMIZE.find((o) => o.value === form.optimize)?.value ?? "—")
      : "—";

  // Show the values that will actually run, not the raw form state. The submit
  // payload uses ``effectiveShardingMode`` (and treats an already-warm DB as
  // satisfied), so the Runtime summary must mirror that — otherwise it reports
  // "off" while a sharded, warm run is queued. See the reconcile effect in
  // BlastSubmit for why ``form`` can lag the effective values.
  const shardingDisplay = runtimeShardingDisplay({
    effectiveShardingMode,
    formShardingMode: form.sharding_mode,
  });
  const warmupDisplay = runtimeWarmupDisplay({
    isDbAlreadyWarm: isDbAlreadyWarm ?? false,
    enableWarmup: form.enable_warmup,
  });

  return (
    <aside className="bsl-rail" aria-label="Search summary">
      {/* ── Input summary block ─────────────────────────────────── */}
      <div className="bsl-rail__group bsl-rail__group--input">
        <h5 className="bsl-rail__group-title">
          <span className="bsl-rail__swatch bsl-rail__swatch--input" />
          Input summary
        </h5>
        <div className="bsl-rail__kv">
          <span className="bsl-rail__k">Program</span>
          <span className="bsl-rail__v">{programMeta.label}</span>
        </div>
        <div className="bsl-rail__kv">
          <span className="bsl-rail__k">Database</span>
          <span className="bsl-rail__v">{dbName}</span>
        </div>
        <div className="bsl-rail__kv">
          <span className="bsl-rail__k">Sequences</span>
          <span className="bsl-rail__v">
            {form.query_data
              ? "entered"
              : form.query_accession.trim()
                ? `${form.query_accession.trim()}${
                    form.query_from.trim() && form.query_to.trim()
                      ? ` (${form.query_from.trim()}–${form.query_to.trim()})`
                      : ""
                  } · fetch at submit`
                : "0"}
          </span>
        </div>
        <div className="bsl-rail__kv">
          <span className="bsl-rail__k">Taxon filter</span>
          <span className="bsl-rail__v">{form.taxid_label || "—"}</span>
        </div>
        <div className="bsl-rail__full-value">
          <span className="bsl-rail__k">Job title</span>
          <span className="bsl-rail__full-text">{form.job_title || "— (auto)"}</span>
        </div>
      </div>

      {/* ── Runtime summary block ───────────────────────────────── */}
      <div className="bsl-rail__group bsl-rail__group--runtime">
        <h5 className="bsl-rail__group-title">
          <span className="bsl-rail__swatch bsl-rail__swatch--runtime" />
          Runtime summary
        </h5>
        {form.program === "blastn" && (
          <div className="bsl-rail__kv">
            <span className="bsl-rail__k">Task</span>
            <span className="bsl-rail__v">{optimizeLabel}</span>
          </div>
        )}
        <div className="bsl-rail__kv">
          <span className="bsl-rail__k">Cluster</span>
          <span className="bsl-rail__v">{form.selectedCluster || "—"}</span>
        </div>
        <div className="bsl-rail__kv">
          <span className="bsl-rail__k">Warmup</span>
          <span className="bsl-rail__v">{warmupDisplay}</span>
        </div>
        <div className="bsl-rail__kv">
          <span className="bsl-rail__k">Sharding</span>
          <span className="bsl-rail__v">{shardingDisplay}</span>
        </div>
        <div className="bsl-rail__kv">
          <span className="bsl-rail__k">E-value</span>
          <span className="bsl-rail__v">{form.evalue}</span>
        </div>
        <div className="bsl-rail__kv">
          <span className="bsl-rail__k">Max targets</span>
          <span className="bsl-rail__v">{form.max_target_seqs}</span>
        </div>
        <div className="bsl-rail__kv">
          <span className="bsl-rail__k">Output fmt</span>
          <span className="bsl-rail__v">{form.outfmt}</span>
        </div>
      </div>

      {/* ── Readiness block ─────────────────────────────────────── */}
      <div className="bsl-rail__group">
        <h5 className="bsl-rail__group-title bsl-rail__readiness-title">
          Readiness · {readyCount}/{readySteps.length}
        </h5>
        <div className="blast-readiness bsl-rail__readiness">
          {readySteps.map((s) => (
            <span
              key={s.label}
              className={`blast-readiness__dot${s.ok ? " blast-readiness__dot--ok" : ""}`}
              title={s.label}
            />
          ))}
          <span className="bsl-rail__readiness-status">
            {readySteps
              .filter((s) => !s.ok)
              .map((s) => s.label)
              .join(" · ") || "All ready"}
          </span>
        </div>

        {missing.length > 0 && !submitPending && (
          <div className="blast-checklist bsl-rail__checklist">
            <strong className="bsl-rail__checklist-title">
              Required before submitting
            </strong>
            <ul>
              {missing.map((m) => (
                <li key={m.text}>
                  {m.text}
                  {m.link && (
                    <Link
                      to={m.link}
                      className="bsl-rail__checklist-link"
                      aria-label={`Go to ${m.text}`}
                    >
                      Go
                    </Link>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* ── Run bar ─────────────────────────────────────────────── */}
      <div className="bsl-rail__runbar">
        {preFlightResult && (
          <PreFlightResultPanel
            result={preFlightResult}
            onPickDb={(path) => set("db", path)}
          />
        )}

        {canSubmit && (
          <BlastCommandPreview
            form={form}
            programMeta={programMeta}
            effectiveSearchSpace={effectiveSearchSpace}
            toast={toast}
          />
        )}

        {canSubmit && (
          <button
            className="glass-button bsl-rail__preflight-btn"
            onClick={onPreFlight}
            disabled={preFlightPending}
          >
            {preFlightPending ? (
              <>
                <Loader2 size={13} className="spin" /> Checking...
              </>
            ) : (
              <>
                <CheckCircle2 size={13} /> Check Readiness
              </>
            )}
          </button>
        )}

        <button
          ref={runBtnRef}
          className={`blast-submit-btn${readyPulse ? " blast-submit-btn--ready-pulse" : ""}`}
          onClick={onSubmit}
          disabled={runDisabled}
          title={runTitle}
        >
          {submitPending ? (
            <Loader2 size={16} strokeWidth={1.5} className="spin" />
          ) : preFlightBlocked ? (
            <ShieldAlert size={15} strokeWidth={1.5} />
          ) : (
            <Play size={15} strokeWidth={1.5} />
          )}
          <span>
            {submitPending
              ? "Submitting"
              : preFlightBlocked
                ? "Resolve blockers"
                : "Run BLAST"}
          </span>
        </button>

        <div
          className="bsl-rail__saved"
          title={
            lastSavedAt
              ? `Draft stored in this browser tab (sessionStorage). Last write: ${lastSavedAt.toLocaleTimeString()}`
              : "Draft will auto-save as you type"
          }
        >
          <Save size={11} strokeWidth={1.5} />
          {formatSavedAgo(lastSavedAt, now)}
          <span className="bsl-rail__saved-shortcut">⌘+Enter</span>
        </div>
      </div>
    </aside>
  );
}
