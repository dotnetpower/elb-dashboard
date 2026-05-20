import { Plus } from "lucide-react";

/**
 * Reusable "Add Cluster" CTA. Renders as a compact header pill when
 * clusters already exist, or as a big dashed CTA when the list is empty.
 */
export function AddClusterButton({
  onClick,
  variant,
}: {
  onClick: () => void;
  variant: "pill" | "dashed";
}) {
  if (variant === "pill") {
    return (
      <button
        type="button"
        onClick={onClick}
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
          cursor: "pointer",
          transition: "border-color 0.15s, color 0.15s",
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.borderColor = "var(--accent)";
          e.currentTarget.style.color = "var(--accent)";
        }}
        onMouseLeave={(e) => {
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
        cursor: "pointer",
        transition: "border-color 0.15s, color 0.15s",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = "var(--accent)";
        e.currentTarget.style.color = "var(--accent)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = "var(--border-medium)";
        e.currentTarget.style.color = "var(--text-muted)";
      }}
    >
      <Plus size={14} strokeWidth={1.5} /> Provision your first cluster
    </button>
  );
}
