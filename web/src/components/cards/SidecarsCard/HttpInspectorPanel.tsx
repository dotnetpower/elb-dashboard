/**
 * HttpInspectorPanel — production wrapper around the critique-hardened
 * Variant A in `./sidecarRequestInspector.tsx`, fed by the live
 * `/api/monitor/sidecar-requests` endpoint.
 *
 * Shape of the upstream response is documented in
 * `api/services/request_metrics.py` (`_DetailSample.to_dict`); sensitive
 * headers are redacted server-side at capture time so the inspector
 * never sees a raw bearer token.
 *
 * Polling cadence: 5 s on first mount and on every refresh button press.
 * The panel is mounted lazily (only when the operator clicks "Inspect
 * HTTP requests" on the SidecarsCard) so we don't pay for the buffer
 * fetch on the dashboard's default render path.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";

import {
  monitoringApi,
  type SidecarRequestSample,
  type SidecarRequestsResponse,
} from "@/api/monitoring";
import {
  type InspectorRequest,
  VariantA,
} from "@/components/cards/SidecarsCard/sidecarRequestInspector";

const REFRESH_INTERVAL_MS = 5_000;
const REQUEST_LIMIT = 200;

function headersToRecord(
  headers: { name: string; value: string }[] | undefined,
): Record<string, string> {
  const out: Record<string, string> = {};
  if (!headers) return out;
  for (const h of headers) {
    if (typeof h?.name === "string" && typeof h?.value === "string") {
      // Last-write-wins for duplicate header names (Set-Cookie etc.) —
      // acceptable for an operator inspector.
      out[h.name] = h.value;
    }
  }
  return out;
}

function degradedSignalFromBody(
  body: string | null | undefined,
): Pick<InspectorRequest, "degraded" | "degradedReasons"> {
  if (!body) return {};

  const parsed = parseJsonPossiblyEncoded(body);
  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    const record = parsed as Record<string, unknown>;
    const degraded = record.degraded === true || record.external_degraded === true;
    if (!degraded) return {};
    const reasons = [
      record.degraded_reason,
      record.external_degraded_reason,
      record.message,
    ].filter(
      (value): value is string => typeof value === "string" && value.trim().length > 0,
    );
    return { degraded: true, degradedReasons: reasons };
  }

  if (/"(?:external_)?degraded"\s*:\s*true/.test(body)) {
    const reasons = [
      ...body.matchAll(
        /"(?:degraded_reason|external_degraded_reason|message)"\s*:\s*"((?:\\.|[^"\\])*)"/g,
      ),
    ]
      .map((match) => match[1].replace(/\\"/g, '"'))
      .filter((value) => value.trim().length > 0);
    return { degraded: true, degradedReasons: reasons };
  }

  return {};
}

function parseJsonPossiblyEncoded(value: string): unknown {
  let current: unknown = value;
  for (let attempt = 0; attempt < 2; attempt++) {
    if (typeof current !== "string") return current;
    try {
      current = JSON.parse(current);
    } catch {
      return null;
    }
  }
  return current;
}

function mapSampleToInspector(s: SidecarRequestSample): InspectorRequest {
  const responseBody = s.response_body
    ? s.response_body_truncated
      ? `${s.response_body}\n… <truncated>`
      : s.response_body
    : undefined;
  const degradedSignal = degradedSignalFromBody(s.response_body);
  return {
    id: s.request_id || `${s.ts}-${s.path}`,
    ts: s.ts * 1000, // backend reports epoch seconds; mockup uses ms
    method: s.method || "GET",
    path: s.path || "",
    status: s.status || 0,
    durationMs: s.duration_ms || 0,
    caller: s.caller ?? "anonymous",
    clientIp: s.client_ip ?? "-",
    requestId: s.request_id || "-",
    requestHeaders: headersToRecord(s.request_headers),
    requestBody: s.request_body
      ? s.request_body_truncated
        ? `${s.request_body}\n… <truncated>`
        : s.request_body
      : undefined,
    responseHeaders: headersToRecord(s.response_headers),
    responseBody,
    responseBytes: s.response_size_bytes ?? 0,
    ...degradedSignal,
  };
}

export function HttpInspectorPanel() {
  const [data, setData] = useState<InspectorRequest[]>([]);
  const [meta, setMeta] = useState<{ count: number; capacity: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [lastFetched, setLastFetched] = useState<number | null>(null);

  const fetchOnce = useCallback(async () => {
    setLoading(true);
    try {
      const resp: SidecarRequestsResponse =
        await monitoringApi.sidecarRequests(REQUEST_LIMIT);
      setData(resp.items.map(mapSampleToInspector));
      setMeta({ count: resp.count, capacity: resp.capacity });
      setError(null);
      setLastFetched(Date.now());
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchOnce();
    const id = window.setInterval(() => {
      if (!document.hidden) void fetchOnce();
    }, REFRESH_INTERVAL_MS);
    const onVisible = () => {
      if (!document.hidden) void fetchOnce();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [fetchOnce]);

  const subtitle = useMemo(() => {
    if (error) return null;
    if (!meta) return "Loading captured requests…";
    return `${meta.count} captured · capacity ${meta.capacity} · refreshes every ${
      REFRESH_INTERVAL_MS / 1000
    }s`;
  }, [meta, error]);

  return (
    <div style={{ marginTop: 12 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 8,
          gap: 8,
        }}
      >
        <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
          {subtitle}
          {lastFetched && (
            <span style={{ marginLeft: 8, color: "var(--text-faint)" }}>
              · last refresh{" "}
              {new Date(lastFetched).toLocaleTimeString(undefined, {
                hour12: false,
              })}
            </span>
          )}
        </div>
        <button
          type="button"
          className="glass-button"
          onClick={() => void fetchOnce()}
          disabled={loading}
          title="Refresh captured requests"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
            padding: "3px 8px",
            fontSize: 10,
          }}
        >
          <RefreshCw
            size={11}
            style={{
              animation: loading ? "spin 1s linear infinite" : "none",
            }}
          />
          Refresh
        </button>
      </div>

      {error && (
        <div
          role="alert"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "8px 10px",
            border: "1px solid var(--danger)",
            borderRadius: 6,
            background: "rgba(224, 123, 138, 0.08)",
            color: "var(--danger)",
            fontSize: 11,
          }}
        >
          <AlertTriangle size={12} />
          <span>Failed to load captured requests: {error}</span>
        </div>
      )}

      {!error && data.length === 0 && !loading && (
        <div
          style={{
            padding: "16px 12px",
            border: "1px dashed var(--border-weak)",
            borderRadius: 6,
            color: "var(--text-muted)",
            fontSize: 11,
            textAlign: "center",
          }}
        >
          No requests captured yet. The inspector buffer is per-process and filters
          streaming/self-inspection routes; it starts populating when non-streaming API
          traffic flows through this api process.
        </div>
      )}

      {!error && data.length > 0 && <VariantA data={data} />}

      <style>{`@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}

export default HttpInspectorPanel;
