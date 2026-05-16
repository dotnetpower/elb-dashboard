import { Briefcase, Server, Shield, type LucideIcon } from "lucide-react";

export const SVC_NAME = "elb-openapi";

export const METHOD_META: Record<string, { color: string; bg: string; glow: string }> = {
  get: {
    color: "#6e9fff",
    bg: "rgba(110,159,255,0.10)",
    glow: "rgba(110,159,255,0.25)",
  },
  post: {
    color: "#73bf69",
    bg: "rgba(115,191,105,0.10)",
    glow: "rgba(115,191,105,0.25)",
  },
  delete: {
    color: "#f2726f",
    bg: "rgba(242,114,111,0.10)",
    glow: "rgba(242,114,111,0.25)",
  },
  put: {
    color: "#f2994a",
    bg: "rgba(242,153,74,0.10)",
    glow: "rgba(242,153,74,0.25)",
  },
  patch: {
    color: "#f2994a",
    bg: "rgba(242,153,74,0.10)",
    glow: "rgba(242,153,74,0.25)",
  },
};

export const TAG_ICONS: Record<string, LucideIcon> = {
  System: Shield,
  Cluster: Server,
  Jobs: Briefcase,
};