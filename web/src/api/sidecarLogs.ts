/**
 * sidecarLogs — typed client for the (planned) per-sidecar live log stream.
 *
 * Backend contract (to be implemented in api/routes/monitor/logs.py):
 *   POST /api/monitor/logs/ticket            -> { ticket, expires_at }
 *   GET  /api/monitor/logs/{container}/events?ticket=…   text/event-stream
 *       event: line   data: { ts, stream, text, level? }
 *       event: drop   data: { count }
 *       :   heartbeat
 *   GET  /api/monitor/logs/{container}/recent?tail=N     -> { lines: [...] }
 *
 * Until that backend lands, the FE uses a local mock generator
 * (see `useSidecarLogs`). The contract here is the source of truth.
 */
import { fetchApiRaw } from "@/api/client";

export const SIDECAR_CONTAINERS = [
  "frontend",
  "api",
  "worker",
  "beat",
  "redis",
  "terminal",
] as const;

export type SidecarContainer = (typeof SIDECAR_CONTAINERS)[number];

export type LogLevel = "DBG" | "INFO" | "WARN" | "ERR" | "OK";

export interface LogLine {
  /** ISO-8601 timestamp (server-side). */
  ts: string;
  /** "stdout" | "stderr" — kept open in case we add color rules later. */
  stream: "stdout" | "stderr";
  /** Parsed log level. May be inferred client-side when the backend
   *  hasn't tagged it. */
  level?: LogLevel;
  /** Raw log text, after server-side sanitization (no bearer tokens, SAS sigs). */
  text: string;
}

export interface SidecarLogsTicket {
  ticket: string;
  expires_at: number; // unix ts in seconds
}

/** POST /api/monitor/logs/ticket → short-lived ticket for SSE. */
export async function requestLogsTicket(): Promise<SidecarLogsTicket> {
  const r = await fetchApiRaw("/monitor/logs/ticket", { method: "POST" });
  if (!r.ok) {
    const err = new Error(`logs ticket: HTTP ${r.status}`) as Error & { status: number };
    err.status = r.status;
    throw err;
  }
  return (await r.json()) as SidecarLogsTicket;
}

/** SSE URL for a given container; pass to `new EventSource(url)`. */
export function sidecarLogsStreamUrl(container: SidecarContainer, ticket: string): string {
  return `/api/monitor/logs/${encodeURIComponent(container)}/events?ticket=${encodeURIComponent(ticket)}`;
}

/** GET /api/monitor/logs/{c}/recent?tail=N — used as backfill / fallback. */
export async function fetchRecentLogs(
  container: SidecarContainer,
  tail: number = 200,
): Promise<LogLine[]> {
  const clamped = Math.max(1, Math.min(2000, Math.floor(tail)));
  const r = await fetchApiRaw(`/monitor/logs/${encodeURIComponent(container)}/recent?tail=${clamped}`);
  if (!r.ok) {
    const err = new Error(`logs recent: HTTP ${r.status}`) as Error & { status: number };
    err.status = r.status;
    throw err;
  }
  const body = (await r.json()) as { lines?: LogLine[] };
  return body.lines ?? [];
}
