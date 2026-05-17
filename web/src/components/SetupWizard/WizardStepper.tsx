import type { Step } from "./types";

export function WizardStepper({ step }: { step: Step }) {
  const stepLabels = ["Account", "Project Folders", "Find Resources", "Confirm"];
  return (
    <div style={{ padding: "20px 32px", display: "flex", alignItems: "center" }}>
      {stepLabels.map((label, i) => {
        const n = (i + 1) as Step;
        const done = n < step;
        const active = n === step;
        return (
          <div key={n} style={{ display: "contents" }}>
            {i > 0 && (
              <div
                style={{
                  flex: 1,
                  height: 2,
                  margin: "0 12px",
                  minWidth: 24,
                  background: done ? "var(--success)" : "var(--border-weak)",
                }}
              />
            )}
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <div
                style={{
                  width: 28,
                  height: 28,
                  borderRadius: "50%",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 12,
                  fontWeight: 700,
                  flexShrink: 0,
                  border: `2px solid ${
                    done
                      ? "var(--success)"
                      : active
                        ? "var(--accent)"
                        : "var(--border-medium)"
                  }`,
                  color: done
                    ? "var(--bg-primary)"
                    : active
                      ? "var(--accent)"
                      : "var(--text-faint)",
                  background: done
                    ? "var(--success)"
                    : active
                      ? "rgba(110,159,255,0.08)"
                      : "transparent",
                }}
              >
                {done ? "✓" : n}
              </div>
              <span
                style={{
                  fontSize: 12,
                  whiteSpace: "nowrap",
                  color: done
                    ? "var(--success)"
                    : active
                      ? "var(--text-primary)"
                      : "var(--text-faint)",
                  fontWeight: active ? 500 : 400,
                }}
              >
                {label}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
