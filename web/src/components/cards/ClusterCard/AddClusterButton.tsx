import { Plus } from "lucide-react";

/**
 * Reusable "Add Cluster" CTA. Renders as a compact header pill when
 * clusters already exist, or as a big dashed CTA when the list is empty.
 *
 * `disabled` is used while another cluster is still provisioning so the
 * user cannot kick off a second concurrent provision from this card.
 */
export function AddClusterButton({
  onClick,
  variant,
  disabled = false,
  disabledTitle,
}: {
  onClick: () => void;
  variant: "pill" | "dashed";
  disabled?: boolean;
  disabledTitle?: string;
}) {
  if (variant === "pill") {
    return (
      <button
        type="button"
        onClick={onClick}
        disabled={disabled}
        title={disabled ? disabledTitle : undefined}
        aria-disabled={disabled || undefined}
        className="dashboard-hide-mobile"
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
          padding: "3px 9px",
          background: "none",
          border: "1px solid var(--border-medium)",
          borderRadius: 999,
          color: "var(--text-muted)",
          fontSize: 11,
          cursor: disabled ? "not-allowed" : "pointer",
          opacity: disabled ? 0.5 : 1,
          transition: "border-color 0.15s, color 0.15s",
        }}
        onMouseEnter={(e) => {
          if (disabled) return;
          e.currentTarget.style.borderColor = "var(--accent)";
          e.currentTarget.style.color = "var(--accent)";
        }}
        onMouseLeave={(e) => {
          if (disabled) return;
          e.currentTarget.style.borderColor = "var(--border-medium)";
          e.currentTarget.style.color = "var(--text-muted)";
        }}
      >
        <Plus size={12} strokeWidth={1.5} /> Add Cluster
      </button>
    );
  }
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={disabled ? disabledTitle : undefined}
      aria-disabled={disabled || undefined}
      className="dashboard-hide-mobile"
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 6,
        width: "100%",
        marginTop: 8,
        padding: "8px 0",
        background: "none",
        border: "1px dashed var(--border-medium)",
        borderRadius: 8,
        color: "var(--text-muted)",
        fontSize: 12,
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.5 : 1,
        transition: "border-color 0.15s, color 0.15s",
      }}
      onMouseEnter={(e) => {
        if (disabled) return;
        e.currentTarget.style.borderColor = "var(--accent)";
        e.currentTarget.style.color = "var(--accent)";
      }}
      onMouseLeave={(e) => {
        if (disabled) return;
        e.currentTarget.style.borderColor = "var(--border-medium)";
        e.currentTarget.style.color = "var(--text-muted)";
      }}
    >
      <Plus size={14} strokeWidth={1.5} /> Provision your first cluster
    </button>
  );
}
