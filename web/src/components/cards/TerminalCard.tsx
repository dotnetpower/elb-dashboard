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
import { ExternalLink, CheckCircle2, AlertTriangle } from "lucide-react";

import { fetchApiRaw } from "@/api/client";
import { MonitorCard } from "@/components/MonitorCard";

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
  const health = useQuery({
    queryKey: ["terminal-sidecar-health"],
    queryFn: fetchTerminalHealth,
    refetchInterval: 30_000,
    retry: false,
  });

  const status = health.data?.status ?? (health.isLoading ? "checking" : "unknown");
  const isOk = status === "ok";
  const dotColor = isOk
    ? "var(--success)"
    : status === "checking"
      ? "var(--text-muted)"
      : "var(--warning)";

  return (
    <MonitorCard
      title="Terminal"
      subtitle="Browser shell with elastic-blast toolchain (in-process sidecar)"
      accentColor="terminal"
      status={isOk ? "ok" : status === "checking" ? "loading" : "error"}
      lastRefreshed={health.dataUpdatedAt ? new Date(health.dataUpdatedAt) : null}
      onRefresh={() => health.refetch()}
      fetching={health.isFetching}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "10px 12px",
            background: "var(--bg-secondary)",
            border: "1px solid var(--border-weak)",
            borderRadius: "var(--radius)",
            fontSize: 12,
          }}
        >
          <span
            style={{
              width: 10,
              height: 10,
              borderRadius: "50%",
              background: dotColor,
              flexShrink: 0,
            }}
          />
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 600 }}>
              {isOk ? (
                <>
                  <CheckCircle2
                    size={12}
                    style={{ display: "inline", marginRight: 4, verticalAlign: -1 }}
                  />
                  Sidecar healthy
                </>
              ) : status === "checking" ? (
                "Checking…"
              ) : (
                <>
                  <AlertTriangle
                    size={12}
                    style={{ display: "inline", marginRight: 4, verticalAlign: -1 }}
                  />
                  Sidecar {status}
                </>
              )}
            </div>
            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
              ttyd loopback{" "}
              <code style={{ fontFamily: "var(--font-mono)" }}>127.0.0.1:7681</code>
              {health.data?.upstream_status ? ` · upstream ${health.data.upstream_status}` : ""}
            </div>
          </div>
        </div>

        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 6,
            fontSize: 11,
            color: "var(--text-muted)",
            lineHeight: 1.5,
          }}
        >
          <div>
            • Authenticated via MSAL bearer + tenant role at the WebSocket upgrade
            (no SSH, no admin password).
          </div>
          <div>
            • Pre-installed: <code>az</code>, <code>kubectl</code>, <code>azcopy</code>,
            <code>python3.11</code>, <code>tmux</code>, <code>elastic-blast</code> venv.
          </div>
          <div>
            • Ephemeral: closing the browser does not stop work (tmux session
            persists for the life of the revision).
          </div>
        </div>

        <Link
          to="/terminal"
          className="glass-button glass-button--primary"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            justifyContent: "center",
            textDecoration: "none",
          }}
        >
          <ExternalLink size={14} />
          Open Terminal
        </Link>
      </div>
    </MonitorCard>
  );
}
