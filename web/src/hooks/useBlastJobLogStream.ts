import { useEffect, useRef, useState } from "react";

import { blastApi } from "@/api/blast";

export interface BlastLogEvent {
  id?: string;
  schema_version?: number;
  job_id: string;
  source: "terminal_exec" | "k8s" | string;
  phase: string;
  stream?: string;
  pod?: string;
  container?: string;
  line: string;
  ts?: string;
}

export interface UseBlastJobLogStreamArgs {
  jobId: string;
  enabled: boolean;
  subscriptionId?: string;
  resourceGroup?: string;
  clusterName?: string;
  namespace?: string;
  tailLines?: number;
}

export interface UseBlastJobLogStreamResult {
  events: BlastLogEvent[];
  source: "connecting" | "live" | "polling";
}

const MAX_EVENTS = 500;
const SSE_RETRY_DELAYS_MS = [2_000, 5_000, 15_000];

export function useBlastJobLogStream({
  jobId,
  enabled,
  subscriptionId,
  resourceGroup,
  clusterName,
  namespace = "default",
  tailLines = 120,
}: UseBlastJobLogStreamArgs): UseBlastJobLogStreamResult {
  const [events, setEvents] = useState<BlastLogEvent[]>([]);
  const [source, setSource] = useState<"connecting" | "live" | "polling">("connecting");
  const seenIdsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    seenIdsRef.current.clear();
    setEvents([]);
  }, [jobId]);

  useEffect(() => {
    if (!enabled || !jobId) {
      setSource("polling");
      return;
    }

    let cancelled = false;
    let attempt = 0;
    let es: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const appendEvent = (event: BlastLogEvent) => {
      const eventId = event.id || `${event.source}:${event.phase}:${event.ts}:${event.line}`;
      if (seenIdsRef.current.has(eventId)) return;
      seenIdsRef.current.add(eventId);
      setEvents((prev) => {
        const next = [...prev, event];
        if (next.length <= MAX_EVENTS) return next;
        const trimmed = next.slice(next.length - MAX_EVENTS);
        seenIdsRef.current = new Set(
          trimmed.map((item) => item.id || `${item.source}:${item.phase}:${item.ts}:${item.line}`),
        );
        return trimmed;
      });
    };

    const scheduleReconnect = () => {
      if (cancelled) return;
      const delay = SSE_RETRY_DELAYS_MS[Math.min(attempt, SSE_RETRY_DELAYS_MS.length - 1)];
      attempt += 1;
      reconnectTimer = setTimeout(connect, delay);
    };

    const connect = async () => {
      if (cancelled) return;
      try {
        const { ticket } = await blastApi.createLogStreamTicket(jobId, {
          subscriptionId,
          resourceGroup,
          clusterName,
          namespace,
          tailLines,
        });
        if (cancelled) return;
        const url = `/api/blast/logs/${encodeURIComponent(jobId)}/events?ticket=${encodeURIComponent(ticket)}`;
        es = new EventSource(url, { withCredentials: false });

        es.addEventListener("log", (ev) => {
          try {
            const payload = JSON.parse((ev as MessageEvent).data) as BlastLogEvent;
            if (payload.line) appendEvent(payload);
            setSource("live");
            attempt = 0;
          } catch {
            // Ignore one malformed frame; the stream remains usable.
          }
        });

        es.addEventListener("completed", () => {
          setSource("polling");
          es?.close();
          es = null;
        });

        es.addEventListener("error", () => {
          setSource("polling");
          es?.close();
          es = null;
          scheduleReconnect();
        });
      } catch {
        setSource("polling");
        scheduleReconnect();
      }
    };

    void connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      es?.close();
    };
  }, [clusterName, enabled, jobId, namespace, resourceGroup, subscriptionId, tailLines]);

  return { events, source };
}