/**
 * Browser terminal — connects to the api sidecar's WebSocket proxy that
 * tunnels to the in-process `terminal` sidecar's loopback ttyd.
 *
 * Flow:
 *   1. POST /api/terminal/ticket  (with MSAL bearer)  -> short-lived ticket
 *   2. WebSocket /api/terminal/ws?ticket=<ticket>     -> bytes <-> ttyd
 *
 * The ticket flow is required because browser WebSocket APIs cannot send
 * Authorization headers; the api validates the bearer once on the HTTP
 * `ticket` call and exchanges it for a single-use token the WebSocket
 * URL can carry.
 */
import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { BookOpen, Sparkles } from "lucide-react";
import "@xterm/xterm/css/xterm.css";

import { fetchApiRaw } from "@/api/client";
import {
  decodeTtydOutputFrame,
  encodeInitialTerminalSize,
  encodeTtydCommandFrame,
} from "@/pages/remoteTerminalProtocol";
import { TerminalManual } from "@/pages/terminal/TerminalManual";
import { TerminalCockpit } from "@/pages/terminal/TerminalCockpit";
import { attachTerminalWheelScroller } from "@/pages/terminal/wheelScroll";

interface TicketResponse {
  ticket: string;
  ttl_seconds: number;
  session_id: string;
  caller: {
    display_name: string;
    upn: string | null;
  };
  shell_user: string;
}

interface TerminalSessionInfo {
  sessionId: string;
  callerDisplay: string;
  shellUser: string;
}

const TERMINAL_TICKET_TIMEOUT_MS = 8_000;

export default function RemoteTerminal() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const [sessionInfo, setSessionInfo] = useState<TerminalSessionInfo | null>(null);
  const [status, setStatus] = useState<"connecting" | "connected" | "disconnected" | "error">(
    "connecting",
  );
  const [error, setError] = useState<string | null>(null);
  const [cockpitOpen, setCockpitOpen] = useState(true);
  const [manualOpen, setManualOpen] = useState(false);

  const handleCopyManualCommand = (command: string) => {
    void navigator.clipboard.writeText(command);
  };

  const handleInsertCommand = (command: string) => {
    termRef.current?.paste(command);
    termRef.current?.focus();
  };

  useEffect(() => {
    if (!containerRef.current) return;
    const term = new Terminal({
      cursorBlink: true,
      cursorStyle: "block",
      fontFamily: '"JetBrains Mono", Menlo, Monaco, "Courier New", monospace',
      fontSize: 14,
      lineHeight: 1.0,
      letterSpacing: 0,
      theme: {
        background: "#0b0e14",
        foreground: "#d4d4d4",
        cursor: "#d4d4d4",
        cursorAccent: "#0b0e14",
        selectionBackground: "#264f78",
        black: "#000000",
        red: "#cd3131",
        green: "#0dbc79",
        yellow: "#e5e510",
        blue: "#2472c8",
        magenta: "#bc3fbc",
        cyan: "#11a8cd",
        white: "#e5e5e5",
        brightBlack: "#666666",
        brightRed: "#f14c4c",
        brightGreen: "#23d18b",
        brightYellow: "#f5f543",
        brightBlue: "#3b8eea",
        brightMagenta: "#d670d6",
        brightCyan: "#29b8db",
        brightWhite: "#e5e5e5",
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(containerRef.current);
    fit.fit();
    termRef.current = term;
    fitRef.current = fit;

    // NOTE: xterm v6 has no ligatures addon yet (the official
    // @xterm/addon-ligatures only supports v5). JBM still ships its
    // OpenType ligature tables, so when an addon becomes available we
    // can wire it in here without touching the rest of the page.

    const ro = new ResizeObserver(() => {
      try {
        fit.fit();
        const ws = wsRef.current;
        if (ws && ws.readyState === WebSocket.OPEN) {
          // ttyd resize protocol: binary frame starting with ASCII "1".
          const msg = JSON.stringify({ columns: term.cols, rows: term.rows });
          ws.send(encodeTtydCommandFrame("1", msg));
        }
      } catch {
        // noop
      }
    });
    ro.observe(containerRef.current);

    let cancelled = false;
    let inputDisposable: { dispose: () => void } | null = null;
    attachTerminalWheelScroller(term);
    let focusTimer: number | null = null;
    let ticketTimeout: number | null = null;
    let reconnectTimer: number | null = null;
    let attempt = 0;
    let connectGeneration = 0;
    let isConnecting = false;
    let connectedOnce = false;
    // Treat the very first attempt as "connecting"; later attempts are
    // automatic recoveries from a dropped ws (terminal sidecar restarted,
    // network blip, idle proxy timeout, etc.).
    //
    // First reconnect is intentionally fast (150 ms) so a `docker compose
    // restart terminal` (or a real Container App revision restart) feels
    // like a flicker rather than a disconnect. Subsequent attempts back
    // off exponentially up to 8 s.
    const RECONNECT_FAST_MS = 150;
    const RECONNECT_BASE_MS = 800;
    const RECONNECT_MAX_MS = 8_000;

    const safeSetError = (message: string | null) => {
      if (!cancelled) setError(message);
    };

    const safeSetStatus = (next: "connecting" | "connected" | "disconnected" | "error") => {
      if (!cancelled) setStatus(next);
    };

    const scheduleReconnect = (reason: string) => {
      if (cancelled) return;
      if (reconnectTimer !== null) return;
      attempt += 1;
      const delay =
        attempt === 1
          ? RECONNECT_FAST_MS
          : Math.min(
              RECONNECT_MAX_MS,
              RECONNECT_BASE_MS * Math.pow(2, Math.min(attempt - 2, 4)),
            );
      if (attempt > 1) {
        const seconds = (delay / 1000).toFixed(1);
        safeSetError(`${reason}; reconnecting in ${seconds}s (attempt ${attempt})…`);
      } else {
        safeSetError("Reconnecting…");
      }
      safeSetStatus("connecting");
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        void connect();
      }, delay);
    };

    const connect = async () => {
      if (cancelled || isConnecting) return;
      isConnecting = true;
      const generation = ++connectGeneration;
      // Discard any stale input handler from the previous ws so input
      // bytes don't get duplicated after a reconnect.
      inputDisposable?.dispose();
      inputDisposable = null;
      const staleWs = wsRef.current;
      if (staleWs && staleWs.readyState !== WebSocket.CLOSED) {
        staleWs.onclose = null;
        staleWs.close();
      }
      wsRef.current = null;
      const ticketController = new AbortController();
      const ticketTimeoutId = window.setTimeout(
        () => ticketController.abort(new Error("Terminal ticket request timed out.")),
        TERMINAL_TICKET_TIMEOUT_MS,
      );
      ticketTimeout = ticketTimeoutId;
      try {
        // 1. Acquire one-shot ticket
        // fetchApiRaw prepends `/api`; pass only the suffix.
        const ticketResp = await fetchApiRaw("/terminal/ticket", {
          method: "POST",
          signal: ticketController.signal,
        });
        if (ticketTimeout !== null) {
          window.clearTimeout(ticketTimeoutId);
          ticketTimeout = null;
        }
        if (cancelled || generation !== connectGeneration) return;
        if (!ticketResp.ok) {
          if (ticketResp.status === 401 || ticketResp.status === 403) {
            // Auth genuinely failed — retrying won't help.
            safeSetStatus("error");
            safeSetError("Authentication failed. Refresh the page and sign in again.");
            return;
          }
          throw new Error(`ticket request failed: HTTP ${ticketResp.status}`);
        }
        const ticketBody = (await ticketResp.json()) as TicketResponse;
        const { ticket } = ticketBody;
        const nextSessionInfo: TerminalSessionInfo = {
          sessionId: ticketBody.session_id,
          callerDisplay: ticketBody.caller.display_name,
          shellUser: ticketBody.shell_user,
        };
        setSessionInfo(nextSessionInfo);
        if (!connectedOnce) {
          term.writeln(
            `\x1b[36mSigned in:\x1b[0m ${nextSessionInfo.callerDisplay}  ` +
              `\x1b[35mShell:\x1b[0m ${nextSessionInfo.shellUser}  ` +
              `\x1b[90mSession ${nextSessionInfo.sessionId}\x1b[0m`,
          );
          term.writeln("");
        }
        if (cancelled || generation !== connectGeneration) return;

        // 2. Open WebSocket
        const wsUrl = new URL(window.location.href);
        wsUrl.protocol = wsUrl.protocol === "https:" ? "wss:" : "ws:";
        wsUrl.pathname = "/api/terminal/ws";
        wsUrl.search = `?ticket=${encodeURIComponent(ticket)}`;
        const ws = new WebSocket(wsUrl.toString(), ["tty"]);
        ws.binaryType = "arraybuffer";
        wsRef.current = ws;

        ws.onopen = () => {
          if (cancelled || generation !== connectGeneration || wsRef.current !== ws) {
            ws.close();
            return;
          }
          const isReconnect = connectedOnce;
          if (isReconnect) term.reset();
          // Successful connect — clear backoff and any reconnect banner.
          attempt = 0;
          connectedOnce = true;
          safeSetStatus("connected");
          safeSetError(null);
          // ttyd expects the first client message to be JSON bytes with the
          // initial terminal size. Subsequent input/resize messages are
          // command-prefixed binary frames.
          ws.send(encodeInitialTerminalSize(term.cols, term.rows));
          // Focus the terminal so keyboard input works immediately.
          focusTimer = window.setTimeout(() => {
            if (!cancelled) term.focus();
          }, 100);
        };
        ws.onmessage = (ev) => {
          if (cancelled || generation !== connectGeneration || wsRef.current !== ws) return;
          if (!(typeof ev.data === "string" || ev.data instanceof ArrayBuffer)) return;
          const payload = decodeTtydOutputFrame(ev.data);
          if (payload !== null) term.write(payload);
        };
        ws.onerror = () => {
          // Browsers fire a generic error event before close; let the
          // close handler decide whether to reconnect (it has the code).
        };
        ws.onclose = (ev) => {
          if (cancelled || generation !== connectGeneration) return;
          if (wsRef.current === ws) wsRef.current = null;
          if (ev.code === 4401) {
            // Auth failure — don't loop. The user must refresh / re-auth.
            safeSetStatus("error");
            safeSetError("Authentication failed. Refresh the page and sign in again.");
            return;
          }
          // 1000 (normal) is what we send from cleanup; cancelled is
          // already true in that case so we never reach here.
          scheduleReconnect(`Disconnected (code ${ev.code})`);
        };

        inputDisposable = term.onData((d) => {
          if (
            generation === connectGeneration &&
            wsRef.current === ws &&
            ws.readyState === WebSocket.OPEN
          ) {
            // ttyd input protocol: binary frame starting with ASCII "0".
            ws.send(encodeTtydCommandFrame("0", d));
          }
        });
      } catch (e) {
        if (ticketTimeout !== null) {
          window.clearTimeout(ticketTimeoutId);
          ticketTimeout = null;
        }
        if (cancelled || generation !== connectGeneration) return;
        // Network / 5xx / transient failure — back off and retry.
        const message = e instanceof Error ? e.message : String(e);
        scheduleReconnect(`Connect failed: ${message}`);
      } finally {
        if (generation === connectGeneration) isConnecting = false;
      }
    };
    void connect();

    return () => {
      cancelled = true;
      connectGeneration += 1;
      if (ticketTimeout !== null) window.clearTimeout(ticketTimeout);
      if (focusTimer !== null) window.clearTimeout(focusTimer);
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      inputDisposable?.dispose();
      ro.disconnect();
      try {
        wsRef.current?.close();
      } catch {
        /* noop */
      }
      try {
        term.dispose();
      } catch {
        /* noop */
      }
    };
  }, []);

  const statusColor =
    status === "connected"
      ? "var(--success)"
      : status === "connecting"
        ? "var(--text-muted)"
        : "var(--danger)";

  return (
    <div
      className="mono-page terminal-page"
      style={{
        display: "flex",
        flexDirection: "column",
        height: "calc(100vh - 80px)",
        gap: 8,
        padding: 12,
      }}
    >
      <div
        className="mono-header terminal-header"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          padding: "8px 12px",
          background: "var(--bg-secondary)",
          border: "1px solid var(--border-weak)",
          borderRadius: "var(--radius)",
          fontSize: 12,
        }}
      >
        <div style={{ fontWeight: 700, fontSize: 20, lineHeight: 1.2 }}>
          ElasticBLAST Terminal
        </div>
        <button
          type="button"
          className="glass-button terminal-manual-toggle"
          onClick={() => setCockpitOpen((isOpen) => !isOpen)}
          aria-expanded={cockpitOpen}
          aria-controls="terminal-cockpit"
          title="Open the terminal cockpit"
        >
          <Sparkles size={13} strokeWidth={1.5} />
          Cockpit
        </button>
        <button
          type="button"
          className="glass-button terminal-manual-toggle"
          onClick={() => setManualOpen((isOpen) => !isOpen)}
          aria-expanded={manualOpen}
          aria-controls="terminal-manual"
          title="Open the terminal manual"
        >
          <BookOpen size={13} strokeWidth={1.5} />
          Manual
        </button>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            color: statusColor,
            fontSize: 11,
          }}
        >
          <span
            style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: statusColor,
              display: "inline-block",
            }}
          />
          {status}
        </div>
        {error && (
          <div style={{ color: "var(--warning)", fontSize: 11, marginLeft: "auto" }}>
            {error}
          </div>
        )}
        <div style={{ marginLeft: error ? 12 : "auto", color: "var(--text-faint)", fontSize: 11 }}>
          {sessionInfo ? (
            <>
              Signed in: <code>{sessionInfo.callerDisplay}</code> · Shell:{" "}
              <code>{sessionInfo.shellUser}</code> · Session: <code>{sessionInfo.sessionId}</code>
            </>
          ) : (
            <>
              Sidecar: <code>terminal</code> · Connection: <code>WSS /api/terminal/ws</code>
            </>
          )}
        </div>
      </div>

      <div
        className={`terminal-workspace${manualOpen || cockpitOpen ? " terminal-workspace--manual" : ""}`}
      >
        {(cockpitOpen || manualOpen) && (
          <div className="terminal-side-panels">
            {cockpitOpen && (
              <div id="terminal-cockpit" className="terminal-cockpit-wrap">
                <TerminalCockpit
                  connectionStatus={status}
                  callerDisplay={sessionInfo?.callerDisplay ?? null}
                  shellUser={sessionInfo?.shellUser ?? null}
                  onCopyCommand={handleCopyManualCommand}
                  onInsertCommand={handleInsertCommand}
                />
              </div>
            )}
            {manualOpen && (
              <div id="terminal-manual" className="terminal-manual-wrap">
                <TerminalManual onCopyCommand={handleCopyManualCommand} />
              </div>
            )}
          </div>
        )}
        <div
          className="terminal-frame"
          style={{
            flex: 1,
            background: "#0b0e14",
            border: "1px solid var(--border-weak)",
            borderRadius: "var(--radius)",
            overflow: "hidden",
            padding: 8,
          }}
        >
          <div ref={containerRef} style={{ width: "100%", height: "100%" }} />
        </div>
      </div>
    </div>
  );
}
