import { useCallback, useEffect } from "react";
import { useFocusTrap } from "@/hooks/useFocusTrap";

interface Props {
  open?: boolean;
  title: string;
  message?: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open = true,
  title,
  message,
  confirmLabel = "Confirm",
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
        <div className="glass-dialog__actions">
          <button
            className="glass-button"
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            className="glass-button glass-button--danger"
            onClick={onConfirm}
            aria-label={`Permanently ${confirmLabel.toLowerCase()}`}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
