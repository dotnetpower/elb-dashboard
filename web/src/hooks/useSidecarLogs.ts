/**
 * useSidecarLogs — live log tail for one of the six sidecars.
 *
 * Lifecycle mirrors useSidecarMetrics:
 *   1. POST /api/monitor/logs/ticket          (MSAL bearer) → ticket
 *   2. EventSource /api/monitor/logs/{c}/events?ticket=…
 *      - `event: line` per log line
 *      - `event: drop` when the server-side ring buffer dropped frames
 *      - `:` heartbeat every ~25s
 *   3. On 404 / 410 / connect failure we fall back to a *local mock generator*
 *      so the Live Wall renders something before the backend route exists.
 *      Real failures (5xx, network) instead drive the "polling" state and
 *      retry the ticket with bounded backoff.
 *
 * The hook keeps a bounded in-memory ring (default 60 lines per tile) so a
 * chatty sidecar can't blow up React. Tile UI shows the last ~6 lines —
 * the bigger buffer exists for a future "expand to full panel" mode.
 */
import { useEffect, useRef, useState } from "react";

import {
  type LogLine,
  type SidecarContainer,
  requestLogsTicket,
  sidecarLogsStreamUrl,
} from "@/api/sidecarLogs";

const MAX_BUFFER = 60;
const SSE_RETRY_DELAYS_MS = [4_000, 10_000, 30_000];

export type SidecarLogsSource = "connecting" | "live" | "polling" | "mock";

export interface UseSidecarLogsResult {
  lines: LogLine[];
  source: SidecarLogsSource;
  dropped: number;
  lastLineTs: number | null;
}

export interface UseSidecarLogsOptions {
  /** Pause SSE consumption (the tile becomes "paused"). */
  paused?: boolean;
  /** Override the buffer cap. */
  bufferSize?: number;
}

export function useSidecarLogs(
  container: SidecarContainer,
  options: UseSidecarLogsOptions = {},
): UseSidecarLogsResult {
  const { paused = false, bufferSize = MAX_BUFFER } = options;

  const [lines, setLines] = useState<LogLine[]>([]);
  const [source, setSource] = useState<SidecarLogsSource>("connecting");
  const [dropped, setDropped] = useState(0);
  const [lastLineTs, setLastLineTs] = useState<number | null>(null);

  // Hold paused state in a ref so the SSE / mock loops can read it without
  // tearing down on every toggle. We do tear down on container change.
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  useEffect(() => {
    let cancelled = false;
    let es: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let mockTimer: ReturnType<typeof setInterval> | null = null;
    let attempt = 0;

    const push = (line: LogLine) => {
      if (pausedRef.current) return;
      setLines((prev) => {
        const next = prev.concat(line);
        return next.length > bufferSize ? next.slice(next.length - bufferSize) : next;
      });
      const t = Date.parse(line.ts);
      if (!Number.isNaN(t)) setLastLineTs(t);
    };

    // ---- Mock generator -------------------------------------------------
    // The backend route doesn't exist yet — when ticket fails with 404/410,
    // we run a local generator so the Live Wall is demonstrable. Replace
    // entirely once /api/monitor/logs/{c}/events lands.
    const startMock = () => {
      if (cancelled || mockTimer) return;
      setSource("mock");
      const seed = MOCK_SEEDS[container] ?? MOCK_SEEDS.api;
      let i = Math.floor(Math.random() * seed.length);
      const emit = () => {
        if (cancelled) return;
        const sample = seed[i % seed.length];
        i += 1;
        push({
          ts: new Date().toISOString(),
          stream: sample.stream,
          level: sample.level,
          text: sample.text,
        });
      };
      // First two lines immediately so the tile is non-empty on mount.
      emit();
      emit();
      mockTimer = setInterval(emit, 1_400 + Math.random() * 1_800);
    };

    const stopMock = () => {
      if (mockTimer) {
        clearInterval(mockTimer);
        mockTimer = null;
      }
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
        const ticket = await requestLogsTicket();
        if (cancelled) return;
        stopMock();
        const url = sidecarLogsStreamUrl(container, ticket.ticket);
        es = new EventSource(url, { withCredentials: false });

        es.addEventListener("line", (ev) => {
          try {
            const parsed = JSON.parse((ev as MessageEvent).data) as LogLine;
            push(parsed);
            if (source !== "live") setSource("live");
            attempt = 0;
          } catch {
            /* ignore one bad frame */
          }
        });
        es.addEventListener("drop", (ev) => {
          try {
            const parsed = JSON.parse((ev as MessageEvent).data) as { count: number };
            setDropped((d) => d + (parsed.count ?? 0));
          } catch {
            /* ignore */
          }
        });
        es.addEventListener("error", () => {
          setSource((s) => (s === "live" ? "polling" : s));
          es?.close();
          es = null;
          scheduleReconnect();
        });
      } catch (err) {
        const status = (err as { status?: number }).status ?? 0;
        // 404 / 410 ⇒ backend not deployed yet → fall back to mock.
        if (status === 404 || status === 410) {
          startMock();
          return;
        }
        setSource("polling");
        scheduleReconnect();
      }
    };

    void connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      stopMock();
      es?.close();
    };
    // Re-create the SSE / mock pipeline only when the container changes,
    // not when caller-supplied options object identity churns.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [container, bufferSize]);

  return { lines, source, dropped, lastLineTs };
}

// -----------------------------------------------------------------------
// Mock seeds — believable per-sidecar log lines for the FE-first phase.
// -----------------------------------------------------------------------
interface MockSeed {
  stream: "stdout" | "stderr";
  level: LogLine["level"];
  text: string;
}

const MOCK_SEEDS: Record<SidecarContainer, MockSeed[]> = {
  frontend: [
    { stream: "stdout", level: "INFO", text: "200  GET /assets/index-Ckx7.js   8ms" },
    { stream: "stdout", level: "INFO", text: "200  GET /index.html              4ms" },
    { stream: "stdout", level: "INFO", text: "200  GET /favicon.svg             2ms" },
    { stream: "stdout", level: "INFO", text: "200  GET /runtime-config.js       3ms" },
    { stream: "stdout", level: "INFO", text: "200  GET /assets/index-Dz4.css    6ms" },
    { stream: "stdout", level: "INFO", text: "304  GET /assets/icons.svg        1ms" },
  ],
  api: [
    { stream: "stdout", level: "INFO", text: "200  GET  /api/monitor/sidecars     38ms" },
    { stream: "stdout", level: "INFO", text: "200  GET  /api/blast/jobs           22ms" },
    { stream: "stdout", level: "INFO", text: "101  WS   /api/terminal/ws upgraded     " },
    { stream: "stdout", level: "INFO", text: "200  POST /api/audit                14ms" },
    { stream: "stdout", level: "INFO", text: "200  GET  /api/monitor/cluster     117ms" },
    { stream: "stdout", level: "INFO", text: "200  GET  /api/me                    6ms" },
  ],
  worker: [
    { stream: "stdout", level: "INFO", text: "task api.tasks.blast.submit[job-218] received" },
    { stream: "stdout", level: "INFO", text: "staging queries.fa → stelbwork/job-218/queries.fa" },
    { stream: "stderr", level: "WARN", text: "k8s connection reset by peer (attempt 1/3)" },
    { stream: "stderr", level: "ERR",  text: "ResourceNotFoundError: container=results not found" },
    { stream: "stdout", level: "INFO", text: "retry scheduled in 30s (attempt 2/5)" },
    { stream: "stdout", level: "OK",   text: "k8s job-218 first shard pod Ready" },
    { stream: "stdout", level: "INFO", text: "task blast.submit[job-218] retry succeeded" },
  ],
  beat: [
    { stream: "stdout", level: "INFO", text: "tick  monitor.refresh_sidecars" },
    { stream: "stdout", level: "INFO", text: "tick  reconcile.acr_builds" },
    { stream: "stdout", level: "INFO", text: "tick  monitor.refresh_jobs" },
    { stream: "stdout", level: "INFO", text: "tick  reconcile.queue_from_state" },
    { stream: "stdout", level: "INFO", text: "tick  warmup.scheduler_tick" },
  ],
  redis: [
    { stream: "stdout", level: "INFO", text: "PUB  celery → blast.submit" },
    { stream: "stdout", level: "INFO", text: "SUB  worker drained 1 msg" },
    { stream: "stdout", level: "INFO", text: "PUB  celery → warmup.refresh" },
    { stream: "stdout", level: "INFO", text: "SUB  worker drained 1 msg" },
    { stream: "stdout", level: "INFO", text: "PUB  celery → blast.submit (retry)" },
  ],
  terminal: [
    { stream: "stdout", level: "INFO", text: "exec  azcopy copy ./queries.fa <dst>" },
    { stream: "stderr", level: "WARN", text: "azcopy: throughput 0.4 MiB/s < 2 MiB/s" },
    { stream: "stdout", level: "OK",   text: "azcopy completed (8.2 MiB)" },
    { stream: "stdout", level: "INFO", text: "exec  elastic-blast status --cfg job-218" },
    { stream: "stderr", level: "WARN", text: "kubectl: 1 pod CrashLoopBackOff" },
  ],
};
