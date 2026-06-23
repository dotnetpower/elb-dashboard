/**
 * useJobsEvents — open ONE real-time SSE stream and invalidate the job-related
 * React Query caches the instant any job row changes, so the Message Flow card,
 * the Blast Jobs list, and the AKS card Jobs all refetch without waiting out
 * their poll interval.
 *
 * Flow (mirrors useSidecarLogs):
 *   1. POST /api/monitor/jobs-events/ticket (MSAL bearer) → ticket | {enabled:false}
 *   2. EventSource /api/monitor/jobs-events?ticket=…
 *   3. on "jobs-changed" → queryClient.invalidateQueries for the job keys
 *
 * The backend feature is ON by default (env kill-switch JOBS_EVENTS_SSE_DISABLED).
 * When disabled the ticket returns { enabled:false } and this hook stays inert — every card's
 * existing polling is the unchanged fallback. Polling also stays on as a
 * resilience net when the stream drops, so this hook only ever makes the UI
 * faster, never the sole source of freshness.
 *
 * Mount this ONCE at the app/dashboard root.
 */
import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { jobsEventsStreamUrl, requestJobsEventsTicket } from "@/api/jobsEvents";

// Query-key prefixes invalidated on every "jobs-changed" event. invalidateQueries
// matches by prefix, so ["blast-jobs"] covers every ["blast-jobs", sub, rg, …].
const INVALIDATE_KEYS: readonly (readonly unknown[])[] = [
  ["message-flow"],
  ["blast-jobs"],
  ["aks-workload", "jobs"],
];

const RECONNECT_DELAYS_MS = [1_000, 2_000, 5_000, 10_000, 30_000];

export function useJobsEvents(): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    let cancelled = false;
    let es: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    const invalidate = () => {
      for (const queryKey of INVALIDATE_KEYS) {
        void queryClient.invalidateQueries({ queryKey: queryKey as unknown[] });
      }
    };

    const scheduleReconnect = () => {
      if (cancelled) return;
      const delay =
        RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)];
      attempt += 1;
      reconnectTimer = setTimeout(() => void connect(), delay);
    };

    const connect = async () => {
      if (cancelled) return;
      try {
        const ticket = await requestJobsEventsTicket();
        if (cancelled) return;
        // Feature gate off → stay inert; the existing polling is the fallback.
        if (!ticket.enabled || !ticket.ticket) return;
        es = new EventSource(jobsEventsStreamUrl(ticket.ticket), {
          withCredentials: false,
        });
        es.onopen = () => {
          attempt = 0;
        };
        es.addEventListener("jobs-changed", () => {
          invalidate();
        });
        es.addEventListener("error", () => {
          es?.close();
          es = null;
          scheduleReconnect();
        });
      } catch {
        // Ticket failed (network, 5xx, feature missing) → polling remains the
        // source of truth; retry with backoff so a transient blip self-heals.
        scheduleReconnect();
      }
    };

    void connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      es?.close();
    };
  }, [queryClient]);
}
