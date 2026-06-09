import { useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  Loader2,
  Play,
  Save,
  ShieldAlert,
} from "lucide-react";
import { Link } from "react-router-dom";

import { formatApiError } from "@/api/client";
import type { FormState } from "@/pages/blastSubmitModel";
import { BLASTN_OPTIMIZE } from "@/pages/blastSubmitModel";

import { BlastCommandPreview } from "./ui";
import { PreFlightResultPanel } from "./PreFlightResultPanel";
import type { ProgramMeta, ToastFn } from "./types";
import type { MissingItem } from "./submitValidation";
import type { PreFlightResult } from "./usePreFlight";

export interface BlastSubmitFooterProps {
  form: FormState;
  set: <K extends keyof FormState>(key: K, value: FormState[K]) => void;
  programMeta: ProgramMeta;
  toast: ToastFn;
  missing: MissingItem[];
  searchSummary: string;
  canSubmit: boolean;
  submitPending: boolean;
  submitError: unknown;
  preFlightResult: PreFlightResult | null;
  preFlightPending: boolean;
  effectiveSearchSpace?: number;
  /** Wall-clock time of the last successful draft auto-save (N1). */
  lastSavedAt?: Date | null;
  /** When set, overrides the submit-button title with the
   *  "you do not have permission to submit BLAST jobs" tooltip
   *  computed by ``permissionDeniedTooltip``. Critique #6. */
  permissionTooltip?: string;
  onPreFlight: () => void;
  onSubmit: () => void;
}

function formatSavedAgo(when: Date | null | undefined, now: number): string {
  if (!when) return "Draft not saved yet";
  const seconds = Math.max(0, Math.round((now - when.getTime()) / 1000));
  if (seconds < 5) return "Saved just now";
  if (seconds < 60) return `Saved ${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `Saved ${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  return `Saved ${hours}h ago`;
}

export function BlastSubmitFooter({
  form,
  set,
  programMeta,
  toast,
  missing,
  searchSummary,
  canSubmit,
  submitPending,
  submitError,
  preFlightResult,
  preFlightPending,
  effectiveSearchSpace,
  lastSavedAt,
  permissionTooltip,
  onPreFlight,
  onSubmit,
}: BlastSubmitFooterProps) {
  // N1: re-render the "Saved Ns ago" label every 15s so it doesn't go stale
  // while the user idles on the form.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const tick = () => {
      if (!document.hidden) setNow(Date.now());
    };
    const t = window.setInterval(tick, 15_000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      window.clearInterval(t);
      document.removeEventListener("visibilitychange", tick);
    };
  }, []);

  // N2: block the actual Run BLAST button if pre-flight was run and failed.
  // Validation already gates canSubmit on required fields; this adds a second
  // gate that surfaces "fix the readiness checks first" to the user.
  const preFlightBlocked =
    preFlightResult != null && preFlightResult.ready === false;
  const runDisabled = !canSubmit || preFlightBlocked || submitPending;
  // Critique #6: ``permissionTooltip`` wins over the generic
  // "fill in the required fields" hint when the caller lacks
  // ``can_submit_blast`` at the cluster scope, so the user sees WHY
  // submission is blocked.
  const runTitle = permissionTooltip
    ? permissionTooltip
    : preFlightBlocked
      ? `Resolve ${preFlightResult?.critical_blockers ?? 0} pre-flight blocker(s) before submitting`
      : !canSubmit
        ? "Fill in the required fields above"
        : undefined;

  // Focus + pulse the Run BLAST button on the first transition into ready
  // state, skipping focus while the user is still typing in a text field.
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

  return (
    <div className="blast-submit-footer">
      {missing.length > 0 && !submitPending && (
        <div className="blast-checklist">
          <strong className="blast-checklist__title">Required before submitting:</strong>
          <ul>
            {missing.map((m) => (
              <li key={m.text}>
                {m.text}
                {m.link && (
                  <Link
                    to={m.link}
                    className="blast-checklist__link"
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

      {/* Safety net: the Run button is disabled but nothing above explains why
          (no checklist entry, no pre-flight panel). Never leave a greyed-out
          button without an on-screen reason. */}
      {runDisabled && !submitPending && !preFlightBlocked && missing.length === 0 && runTitle && (
        <div className="blast-checklist">
          <strong className="blast-checklist__title">Submission blocked:</strong>
          <ul>
            <li>{runTitle}</li>
          </ul>
        </div>
      )}

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
      <div className="blast-submit-bar">
        <div className="blast-submit-summary">
          {searchSummary && (
            <span className="blast-submit-summary__text">
              {searchSummary}
              {form.optimize && form.program === "blastn" && (
                <span className="muted">
                  {" "}
                  ·{" "}
                  {BLASTN_OPTIMIZE.find((o) => o.value === form.optimize)
                    ?.value ?? ""}
                </span>
              )}
            </span>
          )}
          {/* N1: draft auto-save indicator */}
          <span
            className={`blast-submit-summary__saved${searchSummary ? " blast-submit-summary__saved--offset" : ""}`}
            title={
              lastSavedAt
                ? `Draft stored in this browser tab (sessionStorage). Last write: ${lastSavedAt.toLocaleTimeString()}`
                : "Draft will auto-save as you type"
            }
          >
            <Save size={11} strokeWidth={1.5} />
            {formatSavedAgo(lastSavedAt, now)}
          </span>
        </div>
        <div className="blast-submit-actions">
          {canSubmit && (
            <button
              className="glass-button blast-submit-preflight-btn"
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
        </div>
      </div>
      {submitError != null && (
        <div className="blast-submit-error">
          {formatApiError(submitError, "blast")}
        </div>
      )}
    </div>
  );
}
