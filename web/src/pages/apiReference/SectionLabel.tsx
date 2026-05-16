import type { CSSProperties, ReactNode } from "react";

export function SectionLabel({
  children,
  style,
}: {
  children: ReactNode;
  style?: CSSProperties;
}) {
  return (
    <div
      style={{
        fontSize: 10,
        textTransform: "uppercase",
        letterSpacing: "0.08em",
        color: "var(--text-faint)",
        fontWeight: 700,
        marginBottom: 8,
        ...style,
      }}
    >
      {children}
    </div>
  );
}