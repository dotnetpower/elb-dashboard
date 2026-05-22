/**
 * Per-component build-log viewer used by the upgrade page.
 *
 * Polls `GET /api/upgrade/jobs/{jobId}/build-log/{component}` on a
 * cadence that scales with the active phase: every 3 seconds while the
 * upgrade is in flight, every 30 seconds once it has settled. The blob
 * is replaced in place — the viewer keeps the scroll pinned to the
 * bottom unless the operator manually scrolls up to inspect history.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Copy, RefreshCcw } from "lucide-react";

import { upgradeApi } from "@/api/upgrade";

const ACTIVE_INTERVAL_MS = 3_000;
const IDLE_INTERVAL_MS = 30_000;
const MAX_HEIGHT = 320;
// Cap rendered content to keep the browser snappy on multi-MB logs.
// The full blob is still downloadable via the raw endpoint.
const MAX_RENDER_BYTES = 256 * 1024;

interface Props {
  jobId: string;
  component: "api" | "frontend" | "terminal";
  active: boolean;
}

export function BuildLogViewer({ jobId, component, active }: Props) {
  const [content, setContent] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const viewportRef = useRef<HTMLPreElement | null>(null);

  const fetchOnce = useCallback(async () => {
    setLoading(true);
    try {
      const text = await upgradeApi.buildLog(jobId, component);
      setContent(text);
      setError(null);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "unknown error";
      // 404 is benign when the build hasn't started yet — surface a soft
      // hint instead of a red banner.
      if (msg.includes("404")) {
        setError("No log yet for this component.");
      } else {
        setError(msg);
      }
    } finally {
      setLoading(false);
    }
  }, [jobId, component]);

  useEffect(() => {
    if (!jobId) return undefined;
    void fetchOnce();
    const interval = active ? ACTIVE_INTERVAL_MS : IDLE_INTERVAL_MS;
    const id = window.setInterval(() => {
      void fetchOnce();
    }, interval);
    return () => window.clearInterval(id);
  }, [fetchOnce, active, jobId]);

  useEffect(() => {
    if (autoScroll && viewportRef.current) {
      viewportRef.current.scrollTop = viewportRef.current.scrollHeight;
    }
  }, [content, autoScroll]);

  const handleScroll = (e: React.UIEvent<HTMLPreElement>) => {
    const el = e.currentTarget;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 16;
    setAutoScroll(atBottom);
  };

  return (
    <div style={{ display: "grid", gap: 6 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          fontSize: 12,
        }}
      >
        <strong style={{ flex: 1 }}>{component}</strong>
        <span className="muted" style={{ fontSize: 11 }}>
          {active ? "live (3s)" : "idle (30s)"}
          {loading ? " · loading" : ""}
        </span>
        <button
          type="button"
          className="glass-button"
          onClick={() => void fetchOnce()}
          title="Refresh now"
        >
          <RefreshCcw size={12} strokeWidth={1.5} />
        </button>
        <button
          type="button"
          className="glass-button"
          onClick={() => void navigator.clipboard.writeText(content)}
          title="Copy the full log"
          disabled={!content}
        >
          <Copy size={12} strokeWidth={1.5} />
        </button>
      </div>
      {error && (
        <div className="muted" style={{ fontSize: 11 }}>
          {error}
        </div>
      )}
      {content.length > MAX_RENDER_BYTES && (
        <div className="muted" style={{ fontSize: 11 }}>
          Showing last {Math.round(MAX_RENDER_BYTES / 1024)} KiB of a{" "}
          {Math.round(content.length / 1024)} KiB log — use the copy button for
          the full payload.
        </div>
      )}
      <pre
        ref={viewportRef}
        onScroll={handleScroll}
        style={{
          background: "rgba(0,0,0,0.25)",
          border: "1px solid var(--border-weak)",
          borderRadius: 6,
          padding: 8,
          fontSize: 11,
          lineHeight: 1.45,
          maxHeight: MAX_HEIGHT,
          overflow: "auto",
          margin: 0,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        }}
      >
        {content
          ? content.length > MAX_RENDER_BYTES
            ? content.slice(-MAX_RENDER_BYTES)
            : content
          : loading
            ? "Loading…"
            : "(empty)"}
      </pre>
    </div>
  );
}
