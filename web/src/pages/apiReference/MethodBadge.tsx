import { METHOD_META } from "@/pages/apiReference/constants";

export function MethodBadge({ method, size = "md" }: { method: string; size?: "sm" | "md" }) {
  const meta = METHOD_META[method] || METHOD_META.get;
  const px = size === "sm" ? "6px 8px" : "4px 10px";
  const fs = size === "sm" ? 9 : 10;

  return (
    <span
      style={{
        padding: px,
        borderRadius: 4,
        fontSize: fs,
        fontWeight: 700,
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        color: meta.color,
        background: meta.bg,
        border: `1px solid ${meta.glow}`,
        minWidth: size === "sm" ? 40 : 54,
        textAlign: "center",
        display: "inline-block",
        lineHeight: 1.3,
        fontFamily: "var(--font-mono)",
      }}
    >
      {method}
    </span>
  );
}