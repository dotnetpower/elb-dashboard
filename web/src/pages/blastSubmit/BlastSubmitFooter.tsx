import {
  CheckCircle2,
  Loader2,
  Play,
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
  onPreFlight: () => void;
  onSubmit: () => void;
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
  onPreFlight,
  onSubmit,
}: BlastSubmitFooterProps) {
  return (
    <div className="blast-submit-footer">
      {missing.length > 0 && !submitPending && (
        <div className="blast-checklist">
          <strong style={{ fontSize: 11 }}>Required before submitting:</strong>
          <ul>
            {missing.map((m) => (
              <li key={m.text}>
                {m.text}
                {m.link && (
                  <Link
                    to={m.link}
                    style={{
                      marginLeft: 6,
                      color: "var(--accent)",
                      fontSize: 11,
                    }}
                  >
                    Go →
                  </Link>
                )}
              </li>
            ))}
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
        <BlastCommandPreview form={form} programMeta={programMeta} toast={toast} />
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
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {canSubmit && (
            <button
              className="glass-button"
              onClick={onPreFlight}
              disabled={preFlightPending}
              style={{ fontSize: 12, gap: 5 }}
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
            className="blast-submit-btn"
            onClick={onSubmit}
            disabled={!canSubmit}
          >
            {submitPending ? (
              <Loader2 size={16} strokeWidth={1.5} className="spin" />
            ) : (
              <Play size={15} strokeWidth={1.5} />
            )}
            <span>{submitPending ? "Submitting" : "Run BLAST"}</span>
          </button>
        </div>
      </div>
      {submitError != null && (
        <div style={{ color: "var(--danger)", fontSize: 12, marginTop: 6 }}>
          {formatApiError(submitError, "blast")}
        </div>
      )}
    </div>
  );
}
