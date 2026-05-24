import { useEffect, useRef } from "react";
import {
  FlaskConical,
  FileText,
  Database,
  Bug,
  Gauge,
  Server,
  SlidersHorizontal,
} from "lucide-react";

/* ─── Step definitions ────────────────────────────────────────────── */

export interface StepDef {
  step: number;
  label: string;
  group: "input" | "runtime";
  optional?: boolean;
  icon: React.ReactNode;
}

const ICON_SIZE = 14;
const ICON_STROKE = 1.5;

export const STEPS: StepDef[] = [
  {
    step: 1,
    label: "Program",
    group: "input",
    icon: <FlaskConical size={ICON_SIZE} strokeWidth={ICON_STROKE} />,
  },
  {
    step: 2,
    label: "Search set",
    group: "input",
    icon: <Database size={ICON_SIZE} strokeWidth={ICON_STROKE} />,
  },
  {
    step: 3,
    label: "Query sequence",
    group: "input",
    icon: <FileText size={ICON_SIZE} strokeWidth={ICON_STROKE} />,
  },
  {
    step: 4,
    label: "Taxonomy filter",
    group: "input",
    optional: true,
    icon: <Bug size={ICON_SIZE} strokeWidth={ICON_STROKE} />,
  },
  {
    step: 5,
    label: "Task profile",
    group: "runtime",
    icon: <Gauge size={ICON_SIZE} strokeWidth={ICON_STROKE} />,
  },
  {
    step: 6,
    label: "Execution profile",
    group: "runtime",
    icon: <Server size={ICON_SIZE} strokeWidth={ICON_STROKE} />,
  },
  {
    step: 7,
    label: "Algorithm params",
    group: "runtime",
    optional: true,
    icon: <SlidersHorizontal size={ICON_SIZE} strokeWidth={ICON_STROKE} />,
  },
];

/* ─── Stepper component ──────────────────────────────────────────── */

export interface SubmitStepperProps {
  /** Steps whose "ok" flag is true are rendered as completed. */
  readySteps: { ok: boolean; label: string }[];
  /** Currently focused step (1-based). */
  activeStep: number;
  onStepClick: (step: number) => void;
}

export function SubmitStepper({
  readySteps,
  activeStep,
  onStepClick,
}: SubmitStepperProps) {
  const activeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [activeStep]);

  // Map readiness (Config, Sequence, Database, Taxonomy, Cluster) to per-step done state.
  // Taxonomy is optional: an empty filter is ready, while an invalid filter is not.
  const stepDone = (s: StepDef): boolean => {
    switch (s.step) {
      case 1:
        return true; // program is always selected
      case 2:
        return readySteps[2]?.ok ?? false; // Database
      case 3:
        return readySteps[1]?.ok ?? false; // Sequence
      case 4:
        return readySteps[3]?.ok ?? true; // Taxonomy
      case 5:
        return true; // optimize always has a default
      case 6:
        return readySteps[4]?.ok ?? false; // Cluster
      case 7:
        return true; // params always have defaults
      default:
        return false;
    }
  };

  const inputSteps = STEPS.filter((s) => s.group === "input");
  const runtimeSteps = STEPS.filter((s) => s.group === "runtime");
  const inputDone = inputSteps.filter(stepDone).length;
  const runtimeDone = runtimeSteps.filter(stepDone).length;

  return (
    <nav className="bsl-stepper" aria-label="Search steps">
      {/* Input group */}
      <div className="bsl-stepper__group-hd bsl-stepper__group-hd--input">
        <span className="bsl-stepper__swatch bsl-stepper__swatch--input" />① Input
        <span className="bsl-stepper__count">
          {inputDone}/{inputSteps.length}
        </span>
      </div>
      {inputSteps.map((s) => (
        <button
          type="button"
          key={s.step}
          ref={s.step === activeStep ? activeRef : undefined}
          className={
            "bsl-stepper__step bsl-stepper__step--input" +
            (s.step === activeStep ? " bsl-stepper__step--active" : "") +
            (stepDone(s) ? " bsl-stepper__step--done" : "")
          }
          onClick={() => onStepClick(s.step)}
          aria-current={s.step === activeStep ? "step" : undefined}
        >
          <span
            className={
              "bsl-stepper__num" + (stepDone(s) ? " bsl-stepper__num--done" : "")
            }
          >
            {stepDone(s) ? "✓" : s.step}
          </span>
          {s.label}
          {s.optional && <span className="bsl-stepper__opt">optional</span>}
        </button>
      ))}

      {/* Runtime group */}
      <div className="bsl-stepper__group-hd bsl-stepper__group-hd--runtime">
        <span className="bsl-stepper__swatch bsl-stepper__swatch--runtime" />② Runtime
        <span className="bsl-stepper__count">
          {runtimeDone}/{runtimeSteps.length}
        </span>
      </div>
      {runtimeSteps.map((s) => (
        <button
          type="button"
          key={s.step}
          ref={s.step === activeStep ? activeRef : undefined}
          className={
            "bsl-stepper__step bsl-stepper__step--runtime" +
            (s.step === activeStep ? " bsl-stepper__step--active" : "") +
            (stepDone(s) ? " bsl-stepper__step--done" : "")
          }
          onClick={() => onStepClick(s.step)}
          aria-current={s.step === activeStep ? "step" : undefined}
        >
          <span
            className={
              "bsl-stepper__num bsl-stepper__num--runtime" +
              (stepDone(s) ? " bsl-stepper__num--done" : "")
            }
          >
            {stepDone(s) ? "✓" : s.step}
          </span>
          {s.label}
          {s.optional && <span className="bsl-stepper__opt">advanced</span>}
        </button>
      ))}
    </nav>
  );
}
