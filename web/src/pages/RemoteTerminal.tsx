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
import { SearchAddon } from "@xterm/addon-search";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { BookOpen, ChevronDown, ChevronUp, Search, Sparkles, Type, X } from "lucide-react";
import { AlertTriangle } from "lucide-react";
import "@xterm/xterm/css/xterm.css";

import { fetchApiRaw } from "@/api/client";
import {
  decodeTtydOutputFrame,
  encodeInitialTerminalSize,
  encodeTtydCommandFrame,
} from "@/pages/remoteTerminalProtocol";
import { TerminalManual } from "@/pages/terminal/TerminalManual";
import { TerminalCockpit } from "@/pages/terminal/TerminalCockpit";
import { normaliseCommandForTerminalInsert } from "@/pages/terminal/terminalCockpitModel";
import { analysePastePayload } from "@/pages/terminal/terminalCockpitModel";

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

type TerminalSidePanel = "cockpit" | "manual" | null;

const TERMINAL_TICKET_TIMEOUT_MS = 8_000;

// Font-size bounds for the in-terminal zoom controls (Ctrl +/-/0 and the
// header buttons). The default mirrors the value passed to `new Terminal`.
const TERMINAL_FONT_MIN = 9;
const TERMINAL_FONT_MAX = 28;
const TERMINAL_FONT_DEFAULT = 14;

// Persisted list of command lines the terminal actually executed this session.
// Shared with the cockpit so the session-chapter ladder reflects real activity
// (typed-and-entered lines plus cockpit inserts that ran). Session-scoped so a
// fresh tab starts clean; capped so the buffer cannot grow without bound.
const EXECUTED_COMMANDS_SS_KEY = "elb.cockpit.executedCommands";
const EXECUTED_COMMANDS_MAX = 100;

function readExecutedCommands(): string[] {
  try {
    const raw = sessionStorage.getItem(EXECUTED_COMMANDS_SS_KEY);
    if (raw == null) return [];
    const parsed = JSON.parse(raw) as unknown;
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

function writeExecutedCommands(commands: string[]): void {
  try {
    sessionStorage.setItem(EXECUTED_COMMANDS_SS_KEY, JSON.stringify(commands));
  } catch {
    /* storage may be unavailable or over quota; persistence is best-effort */
  }
}

// Strip terminal escape sequences (arrow keys, bracketed-paste markers,
// function keys) from a raw onData chunk so the activity line-buffer only sees
// the printable keystrokes and control characters it understands.
function stripAnsiForActivity(chunk: string): string {
  return chunk.replace(/\u001b\[[0-9;?]*[ -/]*[@-~]/g, "").replace(/\u001b[@-Z\\-_]/g, "");
}


export default function RemoteTerminal() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const searchAddonRef = useRef<SearchAddon | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [fontSize, setFontSize] = useState(TERMINAL_FONT_DEFAULT);
  const [pendingPaste, setPendingPaste] = useState<string | null>(null);
  const [sessionInfo, setSessionInfo] = useState<TerminalSessionInfo | null>(null);
  const [status, setStatus] = useState<"connecting" | "connected" | "disconnected" | "error">(
    "connecting",
  );
  const [error, setError] = useState<string | null>(null);
  const [sidePanel, setSidePanel] = useState<TerminalSidePanel>("cockpit");
  // Real executed-command history, fed from the PTY input stream below and
  // surfaced to the cockpit so the session-chapter ladder reflects genuine
  // activity rather than typed-but-unrun previews.
  const [executedCommands, setExecutedCommands] = useState<string[]>(() => readExecutedCommands());
  const inputLineBufferRef = useRef("");

  const handleCopyManualCommand = (command: string) => {
    void navigator.clipboard.writeText(command);
  };

  const handleInsertCommand = (command: string, options?: { run?: boolean }) => {
    const terminal = termRef.current;
    const runnableCommand = normaliseCommandForTerminalInsert(command);
    if (!terminal || status !== "connected" || runnableCommand.length === 0) return;
    terminal.paste(runnableCommand);
    // Default to running the command (legacy behaviour). When the cockpit's
    // "run on insert" toggle is off the command is only typed, so the user can
    // review it and press Enter themselves.
    if (options?.run !== false) {
      terminal.input("\r");
    }
    terminal.focus();
  };

  // Append one genuinely executed command line to the shared history. Both
  // direct typing and cockpit inserts flow through the PTY input stream (and
  // thus through `feedActivityFromInput`), so this is the single recorder.
  const recordExecutedCommand = (raw: string) => {
    const line = raw.trim();
    if (!line) return;
    setExecutedCommands((current) => {
      const next = [...current, line].slice(-EXECUTED_COMMANDS_MAX);
      writeExecutedCommands(next);
      return next;
    });
  };

  // Reconstruct command lines from the raw PTY input stream. A line is recorded
  // when the user (or an insert-and-run) sends Enter. Backspace edits the
  // buffer; Ctrl-C / Ctrl-U abandon it; escape sequences are ignored. This is a
  // heuristic — history recall and tab completion echo via the PTY, not onData,
  // so those lines may be missed; that is acceptable for advancing chapters.
  const feedActivityFromInput = (chunk: string) => {
    const cleaned = stripAnsiForActivity(chunk);
    for (const ch of cleaned) {
      const code = ch.charCodeAt(0);
      if (ch === "\r" || ch === "\n") {
        recordExecutedCommand(inputLineBufferRef.current);
        inputLineBufferRef.current = "";
      } else if (code === 0x7f || code === 0x08) {
        inputLineBufferRef.current = inputLineBufferRef.current.slice(0, -1);
      } else if (code === 0x03 || code === 0x15) {
        inputLineBufferRef.current = "";
      } else if (code >= 0x20) {
        inputLineBufferRef.current += ch;
      }
    }
  };

  // Re-fit the terminal to its container and tell ttyd the new geometry.
  // Shared by the ResizeObserver and the font-zoom controls (changing the
  // font changes cols/rows without changing the container size, so the
  // observer would not fire on its own).
  const syncTerminalResize = () => {
    const terminal = termRef.current;
    const fit = fitRef.current;
    if (!terminal || !fit) return;
    try {
      fit.fit();
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        // ttyd resize protocol: command frame starting with ASCII "1".
        const msg = JSON.stringify({ columns: terminal.cols, rows: terminal.rows });
        ws.send(encodeTtydCommandFrame("1", msg));
      }
    } catch {
      /* noop */
    }
  };

  // Apply an absolute font size (clamped) and re-fit so the grid reflows.
  const applyTerminalFontSize = (next: number) => {
    const terminal = termRef.current;
    if (!terminal) return;
    const clamped = Math.max(TERMINAL_FONT_MIN, Math.min(TERMINAL_FONT_MAX, Math.round(next)));
    terminal.options.fontSize = clamped;
    setFontSize(clamped);
    syncTerminalResize();
    terminal.focus();
  };

  const adjustTerminalFontSize = (delta: number) => {
    const current = termRef.current?.options.fontSize ?? TERMINAL_FONT_DEFAULT;
    applyTerminalFontSize(current + delta);
  };

  // Ctrl+Shift+V style paste: read the clipboard and feed it to the PTY.
  // Multi-line payloads route through a confirmation modal (requestPaste).
  const pasteFromClipboard = async () => {
    const terminal = termRef.current;
    if (!terminal) return;
    try {
      const text = await navigator.clipboard.readText();
      if (text) requestPaste(text);
    } catch {
      /* clipboard read may be blocked in an insecure context; ignore */
    } finally {
      terminal.focus();
    }
  };

  // Single entry point for every paste path (Ctrl+Shift+V, right-click,
  // native Ctrl+V). A multi-line paste would execute several commands the
  // instant it lands, so it is held for explicit confirmation; a single line
  // is pasted straight through.
  const requestPaste = (text: string) => {
    const terminal = termRef.current;
    if (!terminal || text.length === 0) return;
    const analysis = analysePastePayload(text);
    if (analysis.isMultiline) {
      setPendingPaste(text);
      return;
    }
    terminal.paste(text);
    terminal.focus();
  };

  const confirmPendingPaste = () => {
    const terminal = termRef.current;
    if (terminal && pendingPaste !== null) {
      terminal.paste(pendingPaste);
      terminal.focus();
    }
    setPendingPaste(null);
  };

  const cancelPendingPaste = () => {
    setPendingPaste(null);
    termRef.current?.focus();
  };

  const copySelectionToClipboard = (): boolean => {
    const terminal = termRef.current;
    if (!terminal || !terminal.hasSelection()) return false;
    const selection = terminal.getSelection();
    if (selection.length === 0) return false;
    void navigator.clipboard.writeText(selection).catch(() => {
      /* clipboard may be unavailable; ignore */
    });
    return true;
  };

  const runTerminalSearch = (direction: "next" | "previous") => {
    const addon = searchAddonRef.current;
    if (!addon || searchQuery.length === 0) return;
    const options = { caseSensitive: false, wholeWord: false, regex: false };
    if (direction === "next") addon.findNext(searchQuery, options);
    else addon.findPrevious(searchQuery, options);
  };

  const closeTerminalSearch = () => {
    setSearchOpen(false);
    searchAddonRef.current?.clearDecorations();
    termRef.current?.focus();
  };

  // Focus the search box whenever it opens so the user can type immediately.
  useEffect(() => {
    if (searchOpen) {
      const id = window.setTimeout(() => searchInputRef.current?.select(), 0);
      return () => window.clearTimeout(id);
    }
    return undefined;
  }, [searchOpen]);

  // Re-run the highlight as the query changes so matches track keystrokes.
  useEffect(() => {
    const addon = searchAddonRef.current;
    if (!addon) return;
    if (searchQuery.length === 0) {
      addon.clearDecorations();
      return;
    }
    addon.findNext(searchQuery, { caseSensitive: false, wholeWord: false, regex: false });
  }, [searchQuery]);

  // Select-to-copy (Linux/PuTTY style): as soon as a mouse drag selection
  // settles, copy the selected text to the clipboard so the user never has
  // to reach for a keyboard shortcut or the context menu. Wired on the
  // xterm container's `mouseup` so it fires once per selection gesture
  // rather than continuously like `onSelectionChange`.
  const handleTerminalMouseUp = () => {
    const terminal = termRef.current;
    if (!terminal || !terminal.hasSelection()) return;
    const selection = terminal.getSelection();
    if (selection.length > 0) {
      void navigator.clipboard.writeText(selection).catch(() => {
        /* clipboard may be unavailable (insecure context); ignore */
      });
    }
  };

  // PuTTY-style right-click: if there is a selection, copy it; otherwise
  // paste from clipboard. Always suppress the browser context menu.
  // Wired as a native capture-phase listener on the xterm container so it
  // beats xterm's internal canvas/textarea handlers (React's synthetic
  // onContextMenu can be too late for the browser's default menu).
  const handleTerminalContextMenu = (event: Event) => {
    event.preventDefault();
    event.stopPropagation();
    const terminal = termRef.current;
    if (!terminal) return;
    const selection = terminal.getSelection();
    if (selection && selection.length > 0) {
      void navigator.clipboard.writeText(selection).catch(() => {
        /* clipboard may be unavailable; ignore */
      });
      terminal.clearSelection();
      terminal.focus();
      return;
    }
    void (async () => {
      try {
        const text = await navigator.clipboard.readText();
        if (text) requestPaste(text);
      } catch {
        /* clipboard read may be blocked; ignore */
      } finally {
        terminal.focus();
      }
    })();
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
      scrollback: 10_000,
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
    const search = new SearchAddon();
    term.loadAddon(search);
    term.loadAddon(new WebLinksAddon());
    term.open(containerRef.current);
    fit.fit();
    termRef.current = term;
    fitRef.current = fit;
    searchAddonRef.current = search;

    // Keyboard shortcuts handled by the browser side (returning false stops
    // xterm from also sending the keystroke to the PTY):
    //   Ctrl/Cmd+Shift+V  paste from clipboard
    //   Ctrl/Cmd+Shift+C  copy selection (only when there is a selection)
    //   Ctrl/Cmd+Shift+F  toggle the search box
    //   Ctrl/Cmd + +/-/0  font zoom in / out / reset
    term.attachCustomKeyEventHandler((event) => {
      if (event.type !== "keydown") return true;
      const mod = event.ctrlKey || event.metaKey;
      if (!mod) return true;
      const key = event.key.toLowerCase();
      if (event.shiftKey) {
        if (key === "v") {
          event.preventDefault();
          void pasteFromClipboard();
          return false;
        }
        if (key === "c") {
          if (copySelectionToClipboard()) {
            event.preventDefault();
            return false;
          }
          return true;
        }
        if (key === "f") {
          event.preventDefault();
          setSearchOpen(true);
          return false;
        }
        return true;
      }
      // Ctrl/Cmd +/-/0 would otherwise trigger browser page zoom, so we must
      // preventDefault in addition to telling xterm not to forward the key.
      if (key === "+" || key === "=") {
        event.preventDefault();
        adjustTerminalFontSize(1);
        return false;
      }
      if (key === "-" || key === "_") {
        event.preventDefault();
        adjustTerminalFontSize(-1);
        return false;
      }
      if (key === "0") {
        event.preventDefault();
        applyTerminalFontSize(TERMINAL_FONT_DEFAULT);
        return false;
      }
      return true;
    });

    // NOTE: xterm v6 has no ligatures addon yet (the official
    // @xterm/addon-ligatures only supports v5). JBM still ships its
    // OpenType ligature tables, so when an addon becomes available we
    // can wire it in here without touching the rest of the page.

    const ro = new ResizeObserver(() => {
      syncTerminalResize();
    });
    ro.observe(containerRef.current);

    // Capture-phase contextmenu suppression. xterm renders to a canvas
    // and a hidden helper textarea; both can bubble through React but
    // attaching natively in the capture phase guarantees we win over
    // anything xterm or the browser might do.
    const contextMenuTarget = containerRef.current;
    contextMenuTarget.addEventListener("contextmenu", handleTerminalContextMenu, true);
    contextMenuTarget.addEventListener("mouseup", handleTerminalMouseUp);

    // Capture-phase paste guard for native Ctrl+V / browser paste. A
    // multi-line payload is held back from the PTY (preventDefault) and routed
    // to the confirmation modal; single-line pastes fall through to xterm.
    const handleNativePaste = (event: ClipboardEvent) => {
      const text = event.clipboardData?.getData("text") ?? "";
      if (!text) return;
      if (analysePastePayload(text).isMultiline) {
        event.preventDefault();
        event.stopPropagation();
        setPendingPaste(text);
      }
    };
    contextMenuTarget.addEventListener("paste", handleNativePaste, true);

    let cancelled = false;
    let inputDisposable: { dispose: () => void } | null = null;
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
            // Mirror the input into the executed-command history heuristic.
            feedActivityFromInput(d);
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
      contextMenuTarget.removeEventListener("contextmenu", handleTerminalContextMenu, true);
      contextMenuTarget.removeEventListener("mouseup", handleTerminalMouseUp);
      contextMenuTarget.removeEventListener("paste", handleNativePaste, true);
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
    // Mount-once: the terminal/websocket lifecycle is set up a single time and
    // every handler referenced above reaches live state through refs, so the
    // font-zoom/search closures intentionally do not re-run this effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
          fontSize: 12,
        }}
      >
        <div style={{ fontWeight: 700, fontSize: 20, lineHeight: 1.2 }}>
          ElasticBLAST Terminal
        </div>
        <button
          type="button"
          className="glass-button terminal-manual-toggle"
          onClick={() => setSidePanel((current) => (current === "cockpit" ? null : "cockpit"))}
          aria-expanded={sidePanel === "cockpit"}
          aria-controls="terminal-cockpit"
          title="Open the terminal cockpit"
        >
          <Sparkles size={13} strokeWidth={1.5} />
          Cockpit
        </button>
        <button
          type="button"
          className="glass-button terminal-manual-toggle"
          onClick={() => setSidePanel((current) => (current === "manual" ? null : "manual"))}
          aria-expanded={sidePanel === "manual"}
          aria-controls="terminal-manual"
          title="Open the terminal manual"
        >
          <BookOpen size={13} strokeWidth={1.5} />
          Manual
        </button>
        <div className="terminal-toolbar" role="group" aria-label="Terminal view controls">
          <button
            type="button"
            className="glass-button terminal-manual-toggle"
            onClick={() => setSearchOpen((open) => !open)}
            aria-expanded={searchOpen}
            aria-controls="terminal-search"
            title="Search the scrollback (Ctrl+Shift+F)"
          >
            <Search size={13} strokeWidth={1.5} />
            Find
          </button>
          <div className="terminal-font-controls" title="Adjust font size (Ctrl +/-/0)">
            <Type size={13} strokeWidth={1.5} aria-hidden="true" />
            <button
              type="button"
              className="terminal-font-button"
              onClick={() => adjustTerminalFontSize(-1)}
              disabled={fontSize <= TERMINAL_FONT_MIN}
              aria-label="Decrease terminal font size"
            >
              &minus;
            </button>
            <span className="terminal-font-size" aria-live="polite">
              {fontSize}
            </span>
            <button
              type="button"
              className="terminal-font-button"
              onClick={() => adjustTerminalFontSize(1)}
              disabled={fontSize >= TERMINAL_FONT_MAX}
              aria-label="Increase terminal font size"
            >
              +
            </button>
          </div>
        </div>
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
        className={`terminal-workspace${sidePanel ? " terminal-workspace--manual" : ""}`}
      >
        {sidePanel && (
          <div className="terminal-side-panels">
            {sidePanel === "cockpit" && (
              <div id="terminal-cockpit" className="terminal-cockpit-wrap">
                <TerminalCockpit
                  connectionStatus={status}
                  callerDisplay={sessionInfo?.callerDisplay ?? null}
                  shellUser={sessionInfo?.shellUser ?? null}
                  onCopyCommand={handleCopyManualCommand}
                  onInsertCommand={handleInsertCommand}
                  executedCommands={executedCommands}
                />
              </div>
            )}
            {sidePanel === "manual" && (
              <div id="terminal-manual" className="terminal-manual-wrap">
                <TerminalManual
                  onCopyCommand={handleCopyManualCommand}
                  onInsertCommand={handleInsertCommand}
                  canInsertCommand={status === "connected"}
                />
              </div>
            )}
          </div>
        )}
        <div
          className="terminal-frame"
          style={{
            flex: 1,
            position: "relative",
            background: "#0b0e14",
            border: "1px solid var(--border-weak)",
            borderRadius: "var(--radius)",
            overflow: "hidden",
            padding: 8,
          }}
        >
          <div ref={containerRef} style={{ width: "100%", height: "100%" }} />
          {searchOpen && (
            <div id="terminal-search" className="terminal-search" role="search">
              <Search size={13} strokeWidth={1.5} aria-hidden="true" />
              <input
                ref={searchInputRef}
                type="text"
                className="terminal-search__input"
                placeholder="Find in terminal…"
                value={searchQuery}
                onChange={(event) => setSearchQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    runTerminalSearch(event.shiftKey ? "previous" : "next");
                  } else if (event.key === "Escape") {
                    event.preventDefault();
                    closeTerminalSearch();
                  }
                }}
                aria-label="Search the terminal scrollback"
              />
              <button
                type="button"
                className="terminal-search__button"
                onClick={() => runTerminalSearch("previous")}
                disabled={searchQuery.length === 0}
                aria-label="Find previous match"
              >
                <ChevronUp size={14} strokeWidth={1.5} />
              </button>
              <button
                type="button"
                className="terminal-search__button"
                onClick={() => runTerminalSearch("next")}
                disabled={searchQuery.length === 0}
                aria-label="Find next match"
              >
                <ChevronDown size={14} strokeWidth={1.5} />
              </button>
              <button
                type="button"
                className="terminal-search__button"
                onClick={closeTerminalSearch}
                aria-label="Close search"
              >
                <X size={14} strokeWidth={1.5} />
              </button>
            </div>
          )}
        </div>
      </div>
      {pendingPaste !== null && (
        <div
          className="terminal-paste-overlay"
          role="dialog"
          aria-modal="true"
          aria-labelledby="terminal-paste-title"
          onClick={cancelPendingPaste}
        >
          <div className="terminal-paste-modal" onClick={(event) => event.stopPropagation()}>
            <div className="terminal-paste-modal__header">
              <AlertTriangle size={16} strokeWidth={1.5} aria-hidden="true" />
              <span id="terminal-paste-title">Confirm multi-line paste</span>
            </div>
            <p className="terminal-paste-modal__body">
              This paste contains{" "}
              <strong>{analysePastePayload(pendingPaste).lineCount} command lines</strong> and will
              run each line as soon as it is pasted. Review the content before continuing.
            </p>
            <pre className="terminal-paste-modal__preview">{pendingPaste}</pre>
            <div className="terminal-paste-modal__actions">
              <button
                type="button"
                className="glass-button"
                onClick={cancelPendingPaste}
                autoFocus
              >
                Cancel
              </button>
              <button
                type="button"
                className="glass-button glass-button--danger"
                onClick={confirmPendingPaste}
              >
                Paste &amp; run
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
