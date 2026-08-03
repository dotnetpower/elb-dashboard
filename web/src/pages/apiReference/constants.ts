import { Briefcase, Server, Shield, type LucideIcon } from "lucide-react";

export const SVC_NAME = "elb-openapi";

// Method chip palette. `color` resolves through the theme tokens so the
// light palette swaps in its darker, AA-legible inks (the dark-theme
// literals were ~2.1:1 on the light tint backgrounds). The tint/glow
// rgba values stay literal — they read as a faint wash in both themes.
export const METHOD_META: Record<string, { color: string; bg: string; glow: string }> = {
  get: {
    color: "var(--accent)",
    bg: "rgba(110,159,255,0.10)",
    glow: "rgba(110,159,255,0.25)",
  },
  post: {
    color: "var(--success)",
    bg: "rgba(115,191,105,0.10)",
    glow: "rgba(115,191,105,0.25)",
  },
  delete: {
    color: "var(--danger)",
    bg: "rgba(242,114,111,0.10)",
    glow: "rgba(242,114,111,0.25)",
  },
  put: {
    color: "var(--warning)",
    bg: "rgba(242,153,74,0.10)",
    glow: "rgba(242,153,74,0.25)",
  },
  patch: {
    color: "var(--warning)",
    bg: "rgba(242,153,74,0.10)",
    glow: "rgba(242,153,74,0.25)",
  },
};

export const TAG_ICONS: Record<string, LucideIcon> = {
  System: Shield,
  Cluster: Server,
  Jobs: Briefcase,
};