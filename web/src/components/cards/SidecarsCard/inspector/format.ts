/**
 * Sidecar HTTP request inspector — pure formatting + scale helpers.
 *
 * No React. Status/method colour tones, time/byte/latency formatters,
 * log-scale axis helpers, and the `curl` builder. Shared by every
 * presentation module under `inspector/`.
 */

import type { MockReq } from "./types";

export const DEGRADED_COLOR = "#e69b82";
export const DEGRADED_BG = "rgba(230, 155, 130, 0.14)";
export const DEGRADED_RING = "rgba(230, 155, 130, 0.58)";

export function statusTone(code: number): {
  fg: string;
  bg: string;
  ring: string;
  label: string;
} {
  if (code >= 500)
    return {
      fg: "var(--danger)",
      bg: "rgba(224, 123, 138, 0.14)",
      ring: "rgba(224, 123, 138, 0.55)",
      label: "5xx",
    };
  if (code >= 400)
    return {
      fg: "var(--warning)",
      bg: "rgba(240, 198, 116, 0.14)",
      ring: "rgba(240, 198, 116, 0.55)",
      label: "4xx",
    };
  if (code >= 300)
    return {
      fg: "var(--accent)",
      bg: "rgba(122, 167, 255, 0.14)",
      ring: "rgba(122, 167, 255, 0.55)",
      label: "3xx",
    };
  return {
    fg: "var(--success)",
    bg: "rgba(106, 214, 163, 0.14)",
    ring: "rgba(106, 214, 163, 0.55)",
    label: "2xx",
  };
}

export function methodTone(m: string): string {
  if (m === "POST") return "var(--accent)";
  if (m === "DELETE") return "var(--danger)";
  if (m === "PUT") return "var(--warning)";
  return "var(--text-muted)";
}

export function fmtTime(ts: number): string {
  const d = new Date(ts);
  return (
    d.getHours().toString().padStart(2, "0") +
    ":" +
    d.getMinutes().toString().padStart(2, "0") +
    ":" +
    d.getSeconds().toString().padStart(2, "0")
  );
}
export function fmtAgo(ts: number, now: number): string {
  const s = Math.floor((now - ts) / 1000);
  if (s < 60) return `${s}s ago`;
  return `${Math.floor(s / 60)}m ${s % 60}s ago`;
}
export function fmtMs(ms: number): string {
  if (ms >= 1000) return (ms / 1000).toFixed(2) + "s";
  return Math.round(ms) + "ms";
}
export function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}
export function niceLatencyFloor(ms: number): number {
  const candidates = [
    1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000,
  ];
  for (let i = candidates.length - 1; i >= 0; i--) {
    if (candidates[i] <= ms) return candidates[i];
  }
  return candidates[0];
}
export function niceLatencyCeil(ms: number): number {
  const candidates = [
    1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000,
  ];
  return (
    candidates.find((candidate) => candidate >= ms) ?? candidates[candidates.length - 1]
  );
}
export function latencyTicks(minMs: number, maxMs: number): number[] {
  const candidates = [
    1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000,
  ];
  const ticks = candidates.filter(
    (candidate) => candidate >= minMs && candidate <= maxMs,
  );
  if (ticks.length <= 6) return ticks;
  const step = Math.ceil(ticks.length / 6);
  const sampled = ticks.filter((_, index) => index % step === 0);
  const last = ticks[ticks.length - 1];
  return sampled.includes(last) ? sampled : [...sampled, last];
}
export function latencyTone(ms: number): string {
  if (ms >= 2000) return "var(--danger)";
  if (ms >= 500) return "var(--warning)";
  if (ms >= 200) return "var(--text-primary)";
  return "var(--success)";
}

export function requestTone(req: MockReq): ReturnType<typeof statusTone> {
  if (req.degraded && req.status < 400) {
    return {
      fg: DEGRADED_COLOR,
      bg: DEGRADED_BG,
      ring: DEGRADED_RING,
      label: "degraded",
    };
  }
  return statusTone(req.status);
}

export function fmtBytes(b: number): string {
  if (b > 1024) return (b / 1024).toFixed(1) + " KiB";
  return b + " B";
}

export function trianglePoints(cx: number, cy: number, radius: number): string {
  const height = radius * 1.75;
  return [
    `${cx},${cy - height / 2}`,
    `${cx - radius},${cy + height / 2}`,
    `${cx + radius},${cy + height / 2}`,
  ].join(" ");
}

export function headerValue(
  headers: Record<string, string>,
  name: string,
): string | undefined {
  const needle = name.toLowerCase();
  const found = Object.entries(headers).find(([key]) => key.toLowerCase() === needle);
  return found?.[1];
}

export function windowMinLabel(windowStart: number, windowEnd: number): string {
  const minutes = Math.max(1, Math.round((windowEnd - windowStart) / 60_000));
  return `${minutes} min`;
}

export function buildCurl(r: MockReq): string {
  const parts: string[] = [`curl -X ${r.method} 'https://elb.example.com${r.path}'`];
  for (const [k, v] of Object.entries(r.requestHeaders)) {
    // Header values are already redacted in the fixture; in production the
    // backend redacts before serving so the copied curl is always safe.
    parts.push(`  -H '${k}: ${v}'`);
  }
  if (r.requestBody) {
    const body = r.requestBody.replace(/'/g, "'\\''");
    parts.push(`  --data '${body}'`);
  }
  return parts.join(" \\\n");
}
