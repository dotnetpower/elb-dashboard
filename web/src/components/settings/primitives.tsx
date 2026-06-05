import { Activity, AlertCircle, CheckCircle2, Loader2 } from "lucide-react";

/**
 * Shared presentational primitives for the Settings panel.
 *
 * Extracted verbatim from `SettingsPanel.tsx` (issue #24 SRP split). Pure,
 * stateless layout atoms (no panel state, no data fetching) reused across every
 * settings section: structural wrappers (`Section`/`Group`/`Row`/`Field`),
 * inputs (`Segmented`/`Toggle`/`IconButton`), and status chrome
 * (`Badge`/`StatusLine`).
 */

export function Section({ heading, children }: { heading: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.08em", color: "var(--text-faint)", margin: "0 0 12px" }}>{heading}</h3>
      {children}
    </section>
  );
}

export function Group({ title, children }: { title?: string; children: React.ReactNode }) {
  return (
    <div style={{ background: "var(--bg-secondary)", border: "1px solid var(--border-weak)", borderRadius: 8, padding: "0 16px", marginBottom: 14 }}>
      {title && <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-muted)", padding: "12px 0 2px" }}>{title}</div>}
      {children}
    </div>
  );
}

export function Row({ label, hint, control }: { label: React.ReactNode; hint?: React.ReactNode; control: React.ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, padding: "14px 0", borderBottom: "1px solid var(--border-weak)" }}>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 13, color: "var(--text-primary)", marginBottom: 2 }}>{label}</div>
        {hint && <div style={{ fontSize: 12, color: "var(--text-faint)", lineHeight: 1.5 }}>{hint}</div>}
      </div>
      <div style={{ flexShrink: 0 }}>{control}</div>
    </div>
  );
}

export function Field({ label, hint, children }: { label: React.ReactNode; hint?: React.ReactNode; children: React.ReactNode }) {
  return (
    <label style={{ display: "grid", gap: 6, paddingBottom: 10 }}>
      <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{label}</span>
      {children}
      {hint && <span style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.5 }}>{hint}</span>}
    </label>
  );
}

export function Segmented<T extends string>({ value, options, onChange, ariaLabel }: { value: T; options: Array<{ value: T; label: React.ReactNode }>; onChange: (next: T) => void; ariaLabel: string }) {
  return (
    <div role="group" aria-label={ariaLabel} style={{ display: "inline-flex", border: "1px solid var(--border-weak)", background: "var(--bg-tertiary)", borderRadius: 8, padding: 2, gap: 2 }}>
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <button key={option.value} aria-pressed={selected} onClick={() => onChange(option.value)} style={{ display: "inline-flex", alignItems: "center", gap: 6, border: "none", borderRadius: 6, padding: "6px 10px", cursor: "pointer", background: selected ? "var(--bg-hover)" : "transparent", color: selected ? "var(--text-primary)" : "var(--text-muted)", fontSize: 12 }}>
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

export function Toggle({
  checked,
  onChange,
  label,
  disabled,
  describedBy,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
  describedBy?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      aria-describedby={describedBy}
      aria-disabled={disabled || undefined}
      disabled={disabled}
      onClick={() => {
        if (disabled) return;
        onChange(!checked);
      }}
      style={{
        position: "relative",
        width: 36,
        height: 20,
        borderRadius: 999,
        background: checked
          ? "color-mix(in srgb, var(--accent) 30%, var(--bg-tertiary))"
          : "var(--bg-tertiary)",
        border: `1px solid ${checked ? "var(--border-focus)" : "var(--border-medium)"}`,
        cursor: disabled ? "not-allowed" : "pointer",
        padding: 0,
        opacity: disabled ? 0.55 : 1,
      }}
    >
      <span
        style={{
          position: "absolute",
          top: 2,
          left: 2,
          width: 14,
          height: 14,
          borderRadius: "50%",
          background: checked ? "var(--accent)" : "var(--text-muted)",
          transform: checked ? "translateX(16px)" : "translateX(0)",
          transition: "transform 120ms",
        }}
      />
    </button>
  );
}

export function IconButton({
  label,
  onClick,
  children,
  pressed,
  disabled,
  title,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
  pressed?: boolean;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={typeof pressed === "boolean" ? pressed : undefined}
      title={title ?? label}
      onClick={onClick}
      disabled={disabled}
      style={{
        width: 30,
        height: 30,
        display: "grid",
        placeItems: "center",
        color: disabled ? "var(--text-faint)" : "var(--text-muted)",
        background: "var(--bg-tertiary)",
        border: "1px solid var(--border-weak)",
        borderRadius: 6,
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.55 : 1,
      }}
    >
      {children}
    </button>
  );
}

export function Badge({ tone, icon, children }: { tone: "success" | "muted" | "warning"; icon?: React.ReactNode; children: React.ReactNode }) {
  const color =
    tone === "success" ? "var(--success)" : tone === "warning" ? "var(--warning)" : "var(--text-faint)";
  const background =
    tone === "success"
      ? "rgba(115,191,105,0.08)"
      : tone === "warning"
        ? "rgba(229,160,55,0.10)"
        : "var(--bg-tertiary)";
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        fontSize: 11,
        textTransform: "uppercase",
        letterSpacing: "0.04em",
        borderRadius: 999,
        padding: "2px 8px",
        border: "1px solid var(--border-weak)",
        color,
        background,
        whiteSpace: "nowrap",
      }}
    >
      {icon}
      {children}
    </span>
  );
}

export function StatusLine({ kind, children }: { kind: "info" | "success" | "error" | "loading"; children: React.ReactNode }) {
  const icon = kind === "success" ? <CheckCircle2 size={13} color="var(--success)" /> : kind === "error" ? <AlertCircle size={13} color="var(--danger)" /> : kind === "loading" ? <Loader2 size={13} /> : <Activity size={13} />;
  return <div style={{ display: "flex", alignItems: "flex-start", gap: 8, fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5, marginTop: 4 }}><span style={{ marginTop: 1 }}>{icon}</span><span style={{ wordBreak: "break-word", whiteSpace: "pre-wrap" }}>{children}</span></div>;
}
