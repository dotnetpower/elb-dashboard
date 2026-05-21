import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Loader2,
  XCircle,
} from "lucide-react";

import type { PhaseStep, StepState } from "./constants";
import { StepLogBlock } from "./StepLogBlock";

export type StepSubProgress = {
  index: number;
  total: number;
  label?: string;
};

/**
 * Renders a single orchestrator step row: chevron + status icon + label +
 * duration + state badge + (optional) log block + (optional) "extra"
 * children slot (used for file previews).
 */
export function StepRow({
  step,
  state,
  isOpen,
  log,
  duration,
  extra,
  subProgress,
  onToggle,
}: {
  step: PhaseStep;
  state: StepState;
  isOpen: boolean;
  log: string | null;
  duration: string | null;
  extra: React.ReactNode;
  subProgress?: StepSubProgress | null;
  onToggle: () => void;
}) {
  const Icon = step.icon;
  const stateColor =
    state === "done"
      ? "var(--success)"
      : state === "active"
        ? "var(--accent)"
        : state === "error"
          ? "var(--danger)"
          : "var(--text-faint)";

  const isInteractive = state !== "pending";

  return (
    <div
      style={{
        borderRadius: 6,
        overflow: "hidden",
        background: isOpen ? "rgba(255,255,255,0.03)" : "transparent",
        border: isOpen ? "1px solid var(--border-weak)" : "1px solid transparent",
        opacity: state === "skipped" ? 0.5 : 1,
        position: "relative",
      }}
    >
      {/* Shimmer bar at top of active step. */}
      {state === "active" && (
        <div
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            right: 0,
            height: 2,
            overflow: "hidden",
            borderRadius: "6px 6px 0 0",
            pointerEvents: "none",
            background: "rgba(122,167,255,0.10)",
          }}
        >
          <div
            style={{
              position: "absolute",
              top: 0,
              left: 0,
              width: "33%",
              height: "100%",
              background:
                "linear-gradient(90deg, transparent 0%, var(--accent) 50%, transparent 100%)",
              animation: "step-shimmer 1.2s linear infinite",
            }}
          />
        </div>
      )}
      <button
        onClick={isInteractive ? onToggle : undefined}
        disabled={!isInteractive}
        style={{
          all: "unset",
          display: "flex",
          alignItems: "center",
          gap: 10,
          width: "100%",
          padding: "8px 12px",
          cursor: state === "pending" ? "default" : "pointer",
          fontSize: 13,
          boxSizing: "border-box",
        }}
      >
        <span style={{ color: "var(--text-faint)", width: 14, flexShrink: 0 }}>
          {isInteractive ? (
            isOpen ? (
              <ChevronDown size={14} />
            ) : (
              <ChevronRight size={14} />
            )
          ) : null}
        </span>
        <span style={{ flexShrink: 0 }}>
          {state === "done" && (
            <CheckCircle2 size={16} style={{ color: "var(--success)" }} />
          )}
          {state === "active" && (
            <Loader2 size={16} className="spin" style={{ color: "var(--accent)" }} />
          )}
          {state === "error" && (
            <XCircle size={16} style={{ color: "var(--danger)" }} />
          )}
          {state === "skipped" && (
            <Icon size={15} style={{ color: "var(--text-faint)", opacity: 0.5 }} />
          )}
          {state === "pending" && (
            <Icon size={15} style={{ color: "var(--text-faint)", opacity: 0.4 }} />
          )}
        </span>
        <span
          style={{
            flex: 1,
            color: stateColor,
            fontWeight: state === "active" ? 600 : 400,
          }}
        >
          {step.label}
          <span
            style={{
              fontSize: 11,
              marginLeft: 8,
              color: "var(--text-faint)",
              fontWeight: 400,
            }}
          >
            {state === "skipped" ? "Skipped" : step.desc}
          </span>
          {state === "active" && subProgress && subProgress.total > 0 && (
            <span
              title={subProgress.label}
              style={{
                fontSize: 10,
                marginLeft: 8,
                padding: "1px 6px",
                color: "var(--warning, #d8a657)",
                background: "rgba(216,166,103,0.08)",
                border: "1px solid rgba(216,166,103,0.18)",
                borderRadius: 3,
                fontVariantNumeric: "tabular-nums",
                fontWeight: 500,
                letterSpacing: 0.2,
              }}
            >
              {subProgress.index}/{subProgress.total}
              {subProgress.label ? ` · ${subProgress.label}` : ""}
            </span>
          )}
        </span>
        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
          {duration && (
            <span
              style={{
                fontSize: 11,
                color: state === "active" ? "var(--accent)" : "var(--text-faint)",
                fontVariantNumeric: "tabular-nums",
                fontWeight: state === "active" ? 500 : 400,
                minWidth: 28,
                textAlign: "right",
              }}
            >
              {duration}
            </span>
          )}
          {state === "done" && (
            <span style={{ fontSize: 11, color: "var(--success)" }}>✓</span>
          )}
          {state === "skipped" && (
            <span
              style={{
                fontSize: 10,
                color: "var(--text-faint)",
                padding: "1px 6px",
                background: "rgba(255,255,255,0.04)",
                borderRadius: 3,
              }}
            >
              skipped
            </span>
          )}
          {state === "error" && (
            <span
              style={{
                fontSize: 10,
                color: "var(--danger)",
                padding: "1px 6px",
                background: "rgba(224,123,138,0.08)",
                borderRadius: 3,
              }}
            >
              failed
            </span>
          )}
        </span>
      </button>
      {isOpen && log && <StepLogBlock log={log} state={state} stepKey={step.key} />}
      {isOpen && extra && (
        <div
          style={{
            padding: "8px 12px 10px 50px",
            borderTop: log ? "none" : "1px solid var(--border-weak)",
            background: "rgba(0,0,0,0.15)",
          }}
        >
          {extra}
        </div>
      )}
    </div>
  );
}
