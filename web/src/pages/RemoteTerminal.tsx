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
import "@xterm/xterm/css/xterm.css";

import { fetchApiRaw } from "@/api/client";

interface TicketResponse {
  ticket: string;
  ttl_seconds: number;
}

export default function RemoteTerminal() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const [status, setStatus] = useState<"connecting" | "connected" | "disconnected" | "error">(
    "connecting",
  );
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const term = new Terminal({
      cursorBlink: true,
      cursorStyle: "block",
      fontFamily: 'Menlo, Monaco, "Courier New", monospace',
      fontSize: 13,
      lineHeight: 1.2,
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

    const ro = new ResizeObserver(() => {
      try {
        fit.fit();
        const ws = wsRef.current;
        if (ws && ws.readyState === WebSocket.OPEN) {
          // ttyd resize protocol: JSON-encoded text frame starting with "1"
          const msg = JSON.stringify({ columns: term.cols, rows: term.rows });
          ws.send("1" + msg);
        }
      } catch {
        // noop
      }
    });
    ro.observe(containerRef.current);

    let cancelled = false;
    const connect = async () => {
      try {
        // 1. Acquire one-shot ticket
        // fetchApiRaw prepends `/api`; pass only the suffix.
        const ticketResp = await fetchApiRaw("/terminal/ticket", { method: "POST" });
        if (!ticketResp.ok) {
          throw new Error(`ticket request failed: HTTP ${ticketResp.status}`);
        }
        const { ticket } = (await ticketResp.json()) as TicketResponse;
        if (cancelled) return;

        // 2. Open WebSocket
        const wsUrl = new URL(window.location.href);
        wsUrl.protocol = wsUrl.protocol === "https:" ? "wss:" : "ws:";
        wsUrl.pathname = "/api/terminal/ws";
        wsUrl.search = `?ticket=${encodeURIComponent(ticket)}`;
        const ws = new WebSocket(wsUrl.toString(), ["tty"]);
        ws.binaryType = "arraybuffer";
        wsRef.current = ws;

        ws.onopen = () => {
          setStatus("connected");
          // ttyd "0" frame: initial input. Empty payload is fine.
          // ttyd "1" frame: resize.
          const msg = JSON.stringify({ columns: term.cols, rows: term.rows });
          ws.send("1" + msg);
        };
        ws.onmessage = (ev) => {
          if (typeof ev.data === "string") {
            // ttyd output frames start with "0".
            if (ev.data[0] === "0") {
              term.write(ev.data.slice(1));
            } else if (ev.data[0] === "1") {
              // server-initiated resize ack; ignore
            } else {
              term.write(ev.data);
            }
          } else {
            // ArrayBuffer
            const view = new Uint8Array(ev.data);
            if (view.length > 0 && view[0] === 48 /* '0' */) {
              term.write(view.subarray(1));
            } else {
              term.write(view);
            }
          }
        };
        ws.onerror = () => {
          setStatus("error");
          setError("WebSocket error. The terminal sidecar may be unavailable.");
        };
        ws.onclose = (ev) => {
          setStatus("disconnected");
          if (ev.code === 4401) {
            setError("Authentication failed. Refresh and try again.");
          } else if (!error) {
            setError(`Disconnected (code ${ev.code}). Refresh to reconnect.`);
          }
        };

        term.onData((d) => {
          if (ws.readyState === WebSocket.OPEN) {
            // ttyd input frame: "0" + payload
            ws.send("0" + d);
          }
        });
      } catch (e) {
        if (cancelled) return;
        setStatus("error");
        setError(e instanceof Error ? e.message : String(e));
      }
    };
    void connect();

    return () => {
      cancelled = true;
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
      style={{
        display: "flex",
        flexDirection: "column",
        height: "calc(100vh - 80px)",
        gap: 8,
        padding: 12,
      }}
    >
      <div
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
        <div style={{ fontWeight: 600 }}>Terminal</div>
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
          Sidecar: <code>terminal</code> · Connection: <code>WSS /api/terminal/ws</code>
        </div>
      </div>

      <div
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
  );
}
