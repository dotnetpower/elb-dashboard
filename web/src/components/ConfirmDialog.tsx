import { useCallback, useEffect } from "react";
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
  onConfirm,
  onCancel,
}: Props) {
  const trapRef = useFocusTrap<HTMLDivElement>(open);

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
            aria-label={confirmAriaLabel ?? defaultAriaLabel}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
