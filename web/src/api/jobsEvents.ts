/**
 * jobsEvents — typed client for the real-time "jobs changed" SSE stream.
 *
 * Backend contract (api/routes/monitor/jobs_events.py):
 *   POST /api/monitor/jobs-events/ticket  (MSAL bearer)
 *        -> { enabled: true, ticket, expires_at } | { enabled: false }
 *   GET  /api/monitor/jobs-events?ticket=…  text/event-stream
 *        event: jobs-changed  data: { type, reason }
 *        :   heartbeat
 *
 * EventSource cannot send bearer headers, so access is ticket-gated (the ticket
 * POST carries the bearer). The feature is ON by default on the backend (env
 * kill-switch JOBS_EVENTS_SSE_DISABLED); when disabled the ticket call returns
 * { enabled: false } and the SPA keeps polling.
 */
import { fetchApiRaw } from "@/api/client";

export interface JobsEventsTicket {
  enabled: boolean;
  ticket?: string;
  expires_at?: number; // unix ts in seconds
}

/** POST /api/monitor/jobs-events/ticket → ticket (or { enabled:false }). */
export async function requestJobsEventsTicket(): Promise<JobsEventsTicket> {
  const r = await fetchApiRaw("/monitor/jobs-events/ticket", { method: "POST" });
  if (!r.ok) {
    const err = new Error(`jobs-events ticket: HTTP ${r.status}`) as Error & {
      status: number;
    };
    err.status = r.status;
    throw err;
  }
  return (await r.json()) as JobsEventsTicket;
}

/** SSE URL; pass to `new EventSource(url)`. */
export function jobsEventsStreamUrl(ticket: string): string {
  return `/api/monitor/jobs-events?ticket=${encodeURIComponent(ticket)}`;
}
