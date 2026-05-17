import type { SidecarHealth } from "@/hooks/useSidecarMetrics";

import { HEALTH_COLOR } from "./constants";

export interface StatusDotProps {
  health: SidecarHealth;
  size?: number;
}

export function StatusDot({ health, size = 8 }: StatusDotProps) {
  return (
    <span
      aria-hidden
      style={{
        width: size,
        height: size,
        borderRadius: 999,
        background: HEALTH_COLOR[health],
        display: "inline-block",
        flexShrink: 0,
        boxShadow:
          health === "ok" ? `0 0 0 2px rgba(106, 214, 163, 0.18)` : undefined,
      }}
    />
  );
}
