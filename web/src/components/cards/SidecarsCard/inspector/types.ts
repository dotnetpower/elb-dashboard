/**
 * Sidecar HTTP request inspector — shared request shape.
 *
 * The `InspectorRequest` alias is the internal contract between the
 * presentation modules under `inspector/` and the production consumer
 * `HttpInspectorPanel.tsx`. Keep the field set in sync with the backend
 * projection (`GET /api/monitor/sidecar-requests`) when changing it.
 */

export interface MockReq {
  id: string;
  ts: number; // epoch ms
  method: "GET" | "POST" | "DELETE" | "PUT" | string;
  path: string;
  status: number;
  durationMs: number;
  caller: string; // UPN or "anonymous"
  clientIp: string;
  requestId: string;
  requestHeaders: Record<string, string>;
  requestBody?: string;
  responseHeaders: Record<string, string>;
  responseBody?: string;
  responseBytes: number;
  degraded?: boolean;
  degradedReasons?: string[];
}

// Re-exported under a non-mockup name for the production HttpInspectorPanel.
// The shape is an internal contract between this module and that consumer —
// keep them in sync when changing fields.
export type InspectorRequest = MockReq;
