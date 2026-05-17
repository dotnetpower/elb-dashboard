import type { Step } from "./types";

export function WizardFooter({
  step,
  canProceed,
  onBack,
  onNext,
  onFinish,
}: {
  step: Step;
  canProceed: boolean;
  onBack: () => void;
  onNext: () => void;
  onFinish: () => void;
}) {
  return (
    <div
      style={{
        padding: "16px 32px",
        borderTop: "1px solid var(--border-weak)",
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        background: "var(--bg-secondary)",
      }}
    >
      {step > 1 ? (
        <button className="glass-button" onClick={onBack}>
          ← Back
        </button>
      ) : (
        <div />
      )}
      <div style={{ display: "flex", gap: 8 }}>
        {step < 4 ? (
          <button
            className="glass-button glass-button--primary"
            onClick={onNext}
            disabled={!canProceed}
            style={
              !canProceed ? { opacity: 0.4, cursor: "not-allowed" } : undefined
            }
          >
            Next →
          </button>
        ) : (
          <button
            className="glass-button"
            style={{
              background: "rgba(115,191,105,0.12)",
              borderColor: "rgba(115,191,105,0.35)",
              color: "var(--success)",
            }}
            onClick={onFinish}
          >
            Save & Open Dashboard →
          </button>
        )}
      </div>
    </div>
  );
}
