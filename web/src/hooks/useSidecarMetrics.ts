/**
 * useSidecarMetrics — live snapshot of the control-plane sidecars.
 *
 * Connection lifecycle:
 *   1. POST /api/monitor/sidecars/ticket  (MSAL bearer)  -> short-lived ticket
 *   2. EventSource /api/monitor/sidecars/events?ticket=…
 *      - server emits `event: snapshot` every 5 s
 *      - server emits `: heartbeat` every 25 s of idle
 *   3. On any close / error, fall back to 30 s polling of the snapshot
 *      endpoint until the next reconnect attempt succeeds.
 *
 * Why ticket?  Browser EventSource cannot attach Authorization headers
 * (https://github.com/whatwg/html/issues/2177); the ticket pattern mirrors
 * what we already use for /api/terminal/ws so the security review is one
 * pattern, not two.
 *
 * The hook is intentionally keep-it-simple: no exponential backoff library,
 * no ref hellscape — TanStack Query handles the polling fallback, and the
 * SSE attempt is a single useEffect that retries with 5/15/45 s delays.
 */
import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { fetchApiRaw } from "@/api/client";

export type SidecarHealth = "ok" | "degraded" | "down";

export interface SidecarMetric {
  name: string;
  health: SidecarHealth;
  ts: number | null;
  cpu_pct?: number;
  mem_bytes?: number;
  mem_max_bytes?: number | null;
  mem_pct?: number | null;
  host?: string;
  redis_version?: string;
  // Future-proof: any extra field surfaces but we don't render it.
  [key: string]: unknown;
}

export interface SidecarsSnapshot {
  ts: number | null;
  revision: string;
  sidecars: Record<string, SidecarMetric>;
  /**
   * Counts of real events that occurred since the previous snapshot, drained
   * atomically by the api sidecar. The SidecarsCard fires one animated
   * particle along the matching topology row for every count. Missing on
   * older backends — treat as zeros.
   *
   *   row1  Browser → frontend → api      every non-health request
   *   row2  api → redis → worker          every Celery task enqueued by api
   *   row3  beat → redis                   every scheduled task
   *   row4  api ↔ terminal                 every /api/terminal/* request
   */
  events?: {
    row1?: number;
    row2?: number;
    row3?: number;
    row4?: number;
  };
  degraded?: boolean;
  degraded_reason?: string;
}

const QUERY_KEY = ["sidecars-snapshot"] as const;
const POLL_INTERVAL_MS = 30_000;
const LIVE_STALE_MS = 15_000;
const POLLING_STALE_MS = POLL_INTERVAL_MS + 15_000;
const SSE_RETRY_DELAYS_MS = [5_000, 15_000, 45_000];

async function fetchSnapshot(): Promise<SidecarsSnapshot> {
  const r = await fetchApiRaw("/monitor/sidecars", { method: "GET" });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return (await r.json()) as SidecarsSnapshot;
}

async function requestTicket(): Promise<string> {
  const r = await fetchApiRaw("/monitor/sidecars/ticket", { method: "POST" });
  if (!r.ok) throw new Error(`ticket request failed: HTTP ${r.status}`);
  const body = (await r.json()) as { ticket: string };
  return body.ticket;
}

export interface UseSidecarMetricsResult {
  data: SidecarsSnapshot | undefined;
  isLoading: boolean;
  isError: boolean;
  /** "live" = SSE delivering, "polling" = SSE failed and we're falling back. */
  source: "live" | "polling" | "connecting";
  lastUpdated: Date | null;
  isStale: boolean;
  staleAgeMs: number | null;
}

export function useSidecarMetrics(): UseSidecarMetricsResult {
  const queryClient = useQueryClient();
  const [source, setSource] = useState<"live" | "polling" | "connecting">("connecting");
  const [now, setNow] = useState(() => Date.now());
  const sourceRef = useRef(source);
  sourceRef.current = source;

  useEffect(() => {
    const tick = () => {
      if (!document.hidden) setNow(Date.now());
    };
    const timer = setInterval(tick, 5_000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, []);

  // Snapshot fetcher — initial load + polling fallback when SSE is down.
  const query = useQuery<SidecarsSnapshot>({
    queryKey: QUERY_KEY,
    queryFn: fetchSnapshot,
    // Poll only when SSE is *not* delivering. Once SSE attaches, it
    // pushes data into the cache via setQueryData and we disable polling.
    refetchInterval: () => (sourceRef.current === "live" ? false : POLL_INTERVAL_MS),
    staleTime: 5_000,
  });

  // SSE connection — retried with bounded backoff. Aborts on unmount.
  useEffect(() => {
    let cancelled = false;
    let attempt = 0;
    let es: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const scheduleReconnect = () => {
      if (cancelled) return;
      const delay = SSE_RETRY_DELAYS_MS[Math.min(attempt, SSE_RETRY_DELAYS_MS.length - 1)];
      attempt += 1;
      reconnectTimer = setTimeout(connect, delay);
    };

    const connect = async () => {
      if (cancelled) return;
      try {
        const ticket = await requestTicket();
        if (cancelled) return;

        const url = `/api/monitor/sidecars/events?ticket=${encodeURIComponent(ticket)}`;
        es = new EventSource(url, { withCredentials: false });

        es.addEventListener("snapshot", (ev) => {
          try {
            const snap = JSON.parse((ev as MessageEvent).data) as SidecarsSnapshot;
            queryClient.setQueryData(QUERY_KEY, snap);
            if (sourceRef.current !== "live") setSource("live");
            attempt = 0; // healthy frame -> reset backoff
          } catch {
            // bad JSON — ignore one frame, the next will likely be fine
          }
        });

        es.addEventListener("error", () => {
          // Native EventSource auto-reconnects forever. We close manually
          // so we can re-issue a fresh ticket (single-use) and so the
          // polling fallback kicks in instead of silent retries.
          if (sourceRef.current !== "polling") setSource("polling");
          es?.close();
          es = null;
          scheduleReconnect();
        });
      } catch {
        if (sourceRef.current !== "polling") setSource("polling");
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

  const staleAfterMs = source === "live" ? LIVE_STALE_MS : POLLING_STALE_MS;
  const staleAgeMs = query.dataUpdatedAt ? now - query.dataUpdatedAt : null;
  const isStale = Boolean(query.data && staleAgeMs != null && staleAgeMs > staleAfterMs);

  return {
    data: query.data,
    isLoading: query.isLoading,
    isError: (query.isError && source !== "live") || isStale,
    source,
    lastUpdated: query.dataUpdatedAt ? new Date(query.dataUpdatedAt) : null,
    isStale,
    staleAgeMs,
  };
}
