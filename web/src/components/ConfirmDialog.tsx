import { useCallback, useEffect, useId, useState } from "react";
import { useFocusTrap } from "@/hooks/useFocusTrap";

interface Props {
  open?: boolean;
  title: string;
  message?: string;
  details?: string[];
  footnote?: string;
  confirmLabel?: string;
  confirmAriaLabel?: string;
  tone?: "danger" | "primary";
  /** When set, the user must type this exact string into the input
   *  before the confirm button is enabled. Used by destructive flows
   *  (cluster delete) to require an explicit name match. */
  typeToConfirm?: string;
  /** Label rendered above the typed-confirm input (default:
   *  "Type the name to confirm"). */
  typeToConfirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open = true,
  title,
  message,
  details,
  footnote,
  confirmLabel = "Confirm",
  confirmAriaLabel,
  tone = "danger",
  typeToConfirm,
  typeToConfirmLabel,
  onConfirm,
  onCancel,
}: Props) {
  const trapRef = useFocusTrap<HTMLDivElement>(open);
  const inputId = useId();
  const [typed, setTyped] = useState("");

  useEffect(() => {
    // Reset the typed value every time the dialog re-opens so a previous
    // session does not auto-enable Confirm on the next destructive action.
    if (open) setTyped("");
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handleEsc = (e: KeyboardEvent) => { if (e.key === "Escape") onCancel(); };
    window.addEventListener("keydown", handleEsc);
    return () => window.removeEventListener("keydown", handleEsc);
  }, [open, onCancel]);

  const handleBackdropClick = useCallback(
    (e: React.MouseEvent) => { if (e.target === e.currentTarget) onCancel(); },
    [onCancel],
  );

  if (!open) return null;

  const defaultAriaLabel = tone === "danger"
    ? `Permanently ${confirmLabel.toLowerCase()}`
    : confirmLabel;

  const confirmDisabled =
    typeof typeToConfirm === "string" &&
    typeToConfirm.length > 0 &&
    typed.trim() !== typeToConfirm;

  return (
    <div
      className="glass-dialog-backdrop"
      onClick={handleBackdropClick}
      role="dialog"
      aria-modal="true"
      aria-label={title}
      ref={trapRef}
    >
      <div
        className="glass-card glass-card--strong glass-dialog"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 style={{ marginTop: 0 }}>{title}</h3>
        {message && <p className="muted">{message}</p>}
        {details && details.length > 0 && (
          <ul className="muted" style={{ margin: "0 0 12px 0", paddingLeft: 20, lineHeight: 1.6 }}>
            {details.map((line, idx) => (
              <li key={idx}>{line}</li>
            ))}
          </ul>
        )}
        {typeof typeToConfirm === "string" && typeToConfirm.length > 0 && (
          <div style={{ margin: "0 0 12px 0" }}>
            <label
              htmlFor={inputId}
              className="muted"
              style={{ display: "block", fontSize: 12, marginBottom: 6 }}
            >
              {typeToConfirmLabel ?? `Type "${typeToConfirm}" to confirm`}
            </label>
            <input
              id={inputId}
              type="text"
              autoFocus
              autoComplete="off"
              spellCheck={false}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={typeToConfirm}
              aria-describedby={`${inputId}-help`}
              style={{
                width: "100%",
                boxSizing: "border-box",
                padding: "6px 10px",
                fontSize: 13,
                fontFamily:
                  "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
                background: "var(--surface-2, rgba(255,255,255,0.06))",
                color: "var(--text-primary)",
                border: "1px solid var(--border-medium)",
                borderRadius: 6,
                outline: "none",
              }}
            />
            <div
              id={`${inputId}-help`}
              className="muted"
              style={{ fontSize: 11, marginTop: 4 }}
            >
              Must match exactly to enable {confirmLabel}.
            </div>
          </div>
        )}
        {footnote && <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>{footnote}</p>}
        <div className="glass-dialog__actions">
          <button
            className="glass-button"
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            className={`glass-button glass-button--${tone}`}
            onClick={onConfirm}
            disabled={confirmDisabled}
            aria-label={confirmAriaLabel ?? defaultAriaLabel}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
