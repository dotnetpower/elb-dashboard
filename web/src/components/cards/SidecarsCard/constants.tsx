import {
  Boxes,
  Clock,
  Database,
  Globe,
  Server,
  TerminalSquare,
} from "lucide-react";
import type { ReactNode } from "react";

import type { SidecarHealth, SidecarMetric } from "@/hooks/useSidecarMetrics";

export const NODE_W = 168;

export const HEALTH_LABEL: Record<SidecarHealth, string> = {
  ok: "Healthy",
  degraded: "Degraded",
  down: "Down",
};

export const HEALTH_COLOR: Record<SidecarHealth, string> = {
  ok: "var(--success)",
  degraded: "var(--warning)",
  down: "var(--danger)",
};

export const ICONS: Record<string, ReactNode> = {
  frontend: <Globe size={14} strokeWidth={1.5} />,
  api: <Server size={14} strokeWidth={1.5} />,
  worker: <Boxes size={14} strokeWidth={1.5} />,
  beat: <Clock size={14} strokeWidth={1.5} />,
  redis: <Database size={14} strokeWidth={1.5} />,
  terminal: <TerminalSquare size={14} strokeWidth={1.5} />,
};

export const PLACEHOLDER: SidecarMetric = {
  name: "?",
  health: "down",
  ts: null,
};

// Cap per-row particles per snapshot so a sudden burst (e.g. dashboard mount
// firing 6+ ARM/monitor calls in one second) doesn't render a wall of dots.
export const PARTICLES_PER_TICK_CAP = 6;
// Stagger so a multi-event tick reads as a stream rather than overlapping dots.
export const PARTICLE_STAGGER_SEC = 0.18;
// Hard upper bound on the queue. onAnimationEnd is the *primary* removal
// path, but it is not guaranteed (reduced motion, hidden tab, etc.).
export const PARTICLE_QUEUE_HARD_CAP = 64;
// Time budget for a single particle: base CSS duration (1.6 s) + max stagger
// + headroom. After this we force-remove regardless of onAnimationEnd.
export const PARTICLE_LIFETIME_MS = Math.ceil(
  (1.6 + PARTICLES_PER_TICK_CAP * PARTICLE_STAGGER_SEC + 0.6) * 1000,
);
