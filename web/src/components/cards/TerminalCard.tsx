/**
 * Dashboard "Terminal" card.
 *
 * The Container Apps topology has no Remote Terminal VM. The browser
 * terminal is the in-process `terminal` sidecar reached via the
 * authenticated WebSocket proxy at /api/terminal/ws. This card is a
 * lightweight launcher: it shows liveness of the upstream ttyd port and
 * a button to open the terminal page.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ExternalLink, CheckCircle2, Info, AlertTriangle } from "lucide-react";

import { fetchApiRaw } from "@/api/client";
import { MonitorCard } from "@/components/MonitorCard";
import { useAutoRefreshInterval } from "@/hooks/useAutoRefresh";

interface TerminalHealth {
  status: "ok" | "degraded" | "down";
  upstream_status?: number;
  error?: string;
}

async function fetchTerminalHealth(): Promise<TerminalHealth> {
  // fetchApiRaw prepends `/api`; pass only the suffix.
  const r = await fetchApiRaw("/terminal/health", { method: "GET" });
  if (!r.ok) {
    return { status: "down", error: `HTTP ${r.status}` };
  }
  return (await r.json()) as TerminalHealth;
}

export function TerminalCard() {
  const refetchInterval = useAutoRefreshInterval();
  const health = useQuery({
    queryKey: ["terminal-sidecar-health"],
    queryFn: fetchTerminalHealth,
    refetchInterval,
    retry: false,
  });

  const status = health.data?.status ?? (health.isLoading ? "checking" : "unknown");
  const isOk = status === "ok";
  // `down` is the expected state in local dev (no docker-compose / no Container App revision).
  // Render it as a muted "unavailable" rather than a red "error" so it doesn't
  // look like something the user broke.
  const isUnavailable = status === "down";
  const cardStatus = isOk
    ? "ok"
    : status === "checking"
      ? "loading"
      : isUnavailable
        ? "unavailable"
        : "error";
  const dotColor = isOk
    ? "var(--success)"
    : status === "checking"
      ? "var(--text-muted)"
      : isUnavailable
        ? "var(--text-muted)"
        : "var(--warning)";

  return (
    <MonitorCard
      title="Terminal"
      subtitle="Browser shell with elastic-blast toolchain (in-process sidecar)"
      accentColor="terminal"
      status={cardStatus}
      lastRefreshed={health.dataUpdatedAt ? new Date(health.dataUpdatedAt) : null}
      onRefresh={() => health.refetch()}
      fetching={health.isFetching}
    >
      <div style={{ display: "flex", flexDirection: "column" }}>
        <div className="dv3-terminal-status">
          <span
            style={{
              width: 10,
              height: 10,
              borderRadius: "50%",
              background: dotColor,
              flexShrink: 0,
            }}
          />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="lead">
              {isOk ? (
                <>
                  <CheckCircle2 size={14} strokeWidth={1.75} />
                  Sidecar healthy
                </>
              ) : status === "checking" ? (
                "Checking…"
              ) : isUnavailable ? (
                <>
                  <Info
                    size={14}
                    strokeWidth={1.75}
                    style={{ color: "var(--text-muted)" }}
                  />
                  Sidecar unavailable
                </>
              ) : (
                <>
                  <AlertTriangle size={14} strokeWidth={1.75} />
                  Sidecar {status}
                </>
              )}
            </div>
            <div className="sub">
              {isUnavailable ? (
                "Not running in this environment. The terminal sidecar only ships with the deployed Container App (or a local docker-compose stack)."
              ) : (
                <>
                  Listening on <code>127.0.0.1:7681</code>
                  {health.data?.upstream_status
                    ? ` · last probe HTTP ${health.data.upstream_status}`
                    : ""}
                </>
              )}
            </div>
          </div>
        </div>

        <div className="dv3-term-info">
          <div>
            • Authenticated via MSAL bearer + tenant role at the WebSocket upgrade
            (no SSH, no admin password).
          </div>
          <div>
            • Pre-installed: <code>az</code>, <code>kubectl</code>,{" "}
            <code>azcopy</code>, <code>python3.11</code>, <code>tmux</code>,{" "}
            <code>elastic-blast</code> venv.
          </div>
          <div>
            • Ephemeral: closing the browser does not stop work (tmux session
            persists for the life of the revision).
          </div>
        </div>

        {isOk ? (
          <Link to="/terminal" className="dv3-term-cta">
            <ExternalLink size={14} />
            Open Terminal
          </Link>
        ) : (
          <button
            type="button"
            className="dv3-term-cta"
            disabled
            aria-disabled="true"
            title={
              isUnavailable
                ? "Terminal sidecar is not available in this environment"
                : status === "checking"
                  ? "Checking sidecar status…"
                  : `Terminal sidecar is ${status} — cannot open`
            }
          >
            <ExternalLink size={14} />
            Open Terminal
          </button>
        )}
      </div>
    </MonitorCard>
  );
}
