import { useCallback, useEffect, useRef, useState } from "react";

import { fetchApiRawNoRedirect, getApiAccessToken } from "@/api/client";
import { apiBaseUrl } from "@/config/runtime";
import { useClipboardFeedback } from "@/hooks/useClipboardFeedback";

interface OpenApiEndpoint {
  method: string;
  path: string;
  parameters: Array<{ name: string; in: string }>;
  requestBody?: unknown;
}

interface OpenApiProxyInfo {
  sub: string;
  rg: string;
  clusterName: string;
}

export interface OpenApiExecutionResponse {
  status: number;
  body: string;
  time: number;
  /** Content-type the response was served with (used by the manual download
   *  button to pick a sensible blob type / file extension). */
  contentType?: string;
  /** Suggested filename for the manual "Download" button. */
  filename?: string;
  download?: {
    filename: string;
    bytes: number;
    contentType: string;
  };
}

export function useOpenApiExecutor({
  endpoint,
  baseUrl,
  proxyInfo,
  paramValues,
  bodyText,
}: {
  endpoint: OpenApiEndpoint;
  baseUrl: string;
  proxyInfo?: OpenApiProxyInfo;
  paramValues: Record<string, string>;
  bodyText: string;
}) {
  const [response, setResponse] = useState<OpenApiExecutionResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const { copyText } = useClipboardFeedback();
  const mountedRef = useRef(true);
  const requestSeqRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  // Holds the raw bytes of the last response so the manual "Download" button
  // saves the server's original payload, not the viewer's pretty-printed copy.
  // Binary responses store a Blob; text responses store the untouched string.
  const lastPayloadRef = useRef<Blob | string | null>(null);

  useEffect(() => {
    // React 18 StrictMode runs mount → cleanup → re-mount in dev. We must
    // re-arm mountedRef on every mount, otherwise the cleanup from the first
    // pass leaves it false and isCurrent() permanently returns false, which
    // strands the loading spinner.
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  const execute = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const requestSeq = requestSeqRef.current + 1;
    requestSeqRef.current = requestSeq;
    setLoading(true);
    setResponse(null);
    lastPayloadRef.current = null;
    const targetPath = buildTargetPath(endpoint.path, endpoint.parameters, paramValues);
    const start = Date.now();
    const isCurrent = () => mountedRef.current && requestSeqRef.current === requestSeq;
    try {
      const resp = proxyInfo
        ? await executeViaProxy(
            endpoint,
            proxyInfo,
            targetPath,
            bodyText,
            controller.signal,
          )
        : await executeDirect(endpoint, baseUrl, targetPath, bodyText, controller.signal);
      const rendered = await readResponseForViewer(resp, targetPath);
      lastPayloadRef.current = rendered.blob ?? rendered.rawText ?? null;
      if (isCurrent()) {
        setResponse({
          status: resp.status,
          body: rendered.body,
          time: Date.now() - start,
          contentType: rendered.contentType,
          filename: rendered.filename,
          ...(rendered.download ? { download: rendered.download } : {}),
        });
      }
    } catch (e) {
      if (isCurrent() && !isAbortError(e)) {
        setResponse({ status: 0, body: String(e), time: Date.now() - start });
      }
    } finally {
      if (isCurrent()) {
        setLoading(false);
        abortRef.current = null;
      }
    }
  }, [baseUrl, bodyText, endpoint, paramValues, proxyInfo]);

  const copyResponse = useCallback(() => {
    if (response) copyText(response.body, "openapi-response");
  }, [copyText, response]);

  const downloadResponse = useCallback(() => {
    if (!response) return;
    // Save the server's original bytes: a Blob for binary responses, the
    // untouched response text for everything else. Only fall back to the
    // pretty-printed body when no raw payload was captured (e.g. a synthetic
    // network-error response with status 0).
    const payload = lastPayloadRef.current;
    const blob =
      payload instanceof Blob
        ? payload
        : new Blob([payload ?? response.body], {
            type: response.contentType || "text/plain;charset=utf-8",
          });
    const filename = response.filename || "response.txt";
    triggerBrowserDownload(blob, filename);
  }, [response]);

  const copyCurl = useCallback(async () => {
    const origin = typeof window !== "undefined" ? window.location.origin : "";
    let bearerToken: string | null = null;
    if (proxyInfo) {
      try {
        bearerToken = await getApiAccessToken();
      } catch {
        // Fall back to the placeholder if MSAL is unavailable (e.g. not signed
        // in). The copied command is still useful as a template.
        bearerToken = null;
      }
    }
    const curl = buildCurl({
      endpoint,
      baseUrl,
      proxyInfo,
      paramValues,
      bodyText,
      apiBase: apiBaseUrl(),
      origin,
      bearerToken,
    });
    copyText(curl, "openapi-curl");
  }, [baseUrl, bodyText, copyText, endpoint, paramValues, proxyInfo]);

  return { execute, response, loading, copyResponse, downloadResponse, copyCurl };
}

/**
 * Build a `curl` command equivalent to what `execute()` would send.
 *
 * - Proxy mode (cluster-scoped endpoints) → goes through the dashboard's
 *   `/api/aks/openapi/proxy` and needs an MSAL bearer token. When
 *   `bearerToken` is provided it is inlined as-is (caller's responsibility:
 *   the copied command then contains a live credential — handle accordingly).
 *   When omitted, a `$AAD_TOKEN` placeholder is emitted instead so the
 *   command can be pasted in chat / docs without leaking credentials.
 * - Direct mode (`baseUrl` set) → curls the upstream URL straight, no
 *   Authorization header (the upstream has its own auth posture).
 */
export function buildCurl({
  endpoint,
  baseUrl,
  proxyInfo,
  paramValues,
  bodyText,
  apiBase,
  origin,
  bearerToken,
}: {
  endpoint: OpenApiEndpoint;
  baseUrl: string;
  proxyInfo?: OpenApiProxyInfo;
  paramValues: Record<string, string>;
  bodyText: string;
  apiBase: string;
  origin: string;
  bearerToken?: string | null;
}): string {
  const method = endpoint.method.toUpperCase();
  const targetPath = buildTargetPath(endpoint.path, endpoint.parameters, paramValues);
  const hasBody = Boolean(endpoint.requestBody && bodyText);

  let url: string;
  const headers: Array<[string, string]> = [];

  if (proxyInfo) {
    const params = new URLSearchParams({
      subscription_id: proxyInfo.sub,
      resource_group: proxyInfo.rg,
      cluster_name: proxyInfo.clusterName,
      path: targetPath,
    });
    const base = apiBase || origin;
    url = `${base}/api/aks/openapi/proxy?${params.toString()}`;
    headers.push(["Authorization", `Bearer ${bearerToken || "$AAD_TOKEN"}`]);
  } else {
    url = `${baseUrl}${targetPath}`;
  }

  if (hasBody) headers.push(["Content-Type", "application/json"]);

  const parts = [`curl -X ${method} ${shellQuote(url)}`];
  for (const [name, value] of headers) {
    parts.push(`  -H ${shellQuote(`${name}: ${value}`)}`);
  }
  if (hasBody) parts.push(`  --data-raw ${shellQuote(bodyText)}`);
  return parts.join(" \\\n");
}

function shellQuote(value: string): string {
  // POSIX-safe single-quoting: close, escape, reopen.
  return `'${value.replace(/'/g, "'\\''")}'`;
}

export function buildTargetPath(
  path: string,
  parameters: Array<{ name: string; in: string }>,
  paramValues: Record<string, string>,
): string {
  let targetPath = path;
  for (const parameter of parameters.filter((p) => p.in === "path")) {
    targetPath = targetPath.replace(
      `{${parameter.name}}`,
      encodeURIComponent(paramValues[parameter.name] || ""),
    );
  }
  const query = new URLSearchParams();
  for (const parameter of parameters.filter((p) => p.in === "query")) {
    const value = paramValues[parameter.name];
    if (value !== undefined && value !== "") query.append(parameter.name, value);
  }
  const queryString = query.toString();
  if (!queryString) return targetPath;
  return `${targetPath}${targetPath.includes("?") ? "&" : "?"}${queryString}`;
}

async function executeViaProxy(
  endpoint: OpenApiEndpoint,
  proxyInfo: OpenApiProxyInfo,
  targetPath: string,
  bodyText: string,
  signal: AbortSignal,
): Promise<Response> {
  const params = new URLSearchParams({
    subscription_id: proxyInfo.sub,
    resource_group: proxyInfo.rg,
    cluster_name: proxyInfo.clusterName,
    path: targetPath,
  });
  const opts: RequestInit = { method: endpoint.method.toUpperCase(), signal };
  if (endpoint.requestBody && bodyText) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = bodyText;
  }
  return fetchApiRawNoRedirect(`/aks/openapi/proxy?${params.toString()}`, opts);
}

async function executeDirect(
  endpoint: OpenApiEndpoint,
  baseUrl: string,
  targetPath: string,
  bodyText: string,
  signal: AbortSignal,
): Promise<Response> {
  const opts: RequestInit = {
    method: endpoint.method.toUpperCase(),
    headers: { "Content-Type": "application/json" },
    signal,
  };
  if (endpoint.requestBody && bodyText) opts.body = bodyText;
  return fetch(baseUrl + targetPath, opts);
}

export function formatResponseBody(text: string): string {
  if (text.trim().length === 0) {
    return "(empty response body — the server returned 0 bytes)";
  }
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

async function readResponseForViewer(
  resp: Response,
  targetPath: string,
): Promise<{
  body: string;
  contentType: string;
  filename: string;
  /** Untouched response text for text payloads — saved verbatim by the
   *  Download button so the file matches the server's original bytes. */
  rawText?: string;
  blob?: Blob;
  download?: { filename: string; bytes: number; contentType: string };
}> {
  const contentType = resp.headers.get("content-type") ?? "";
  const disposition = resp.headers.get("content-disposition");
  if (resp.ok && isBinaryContentType(contentType)) {
    const blob = await resp.blob();
    const filename = pickDownloadFilename(disposition, contentType, targetPath);
    triggerBrowserDownload(blob, filename);
    const resolvedType = contentType || blob.type || "application/octet-stream";
    return {
      body: formatBinarySummary(filename, blob.size, resolvedType),
      contentType: resolvedType,
      filename,
      blob,
      download: {
        filename,
        bytes: blob.size,
        contentType: resolvedType,
      },
    };
  }
  const text = await resp.text();
  return {
    body: formatResponseBody(text),
    contentType: contentType || "text/plain;charset=utf-8",
    filename: pickDownloadFilename(disposition, contentType, targetPath),
    rawText: text,
  };
}

export function isBinaryContentType(contentType: string): boolean {
  const ct = contentType.split(";")[0].trim().toLowerCase();
  if (!ct) return false;
  if (ct.startsWith("text/")) return false;
  if (ct === "application/json" || ct === "application/problem+json") return false;
  if (ct.endsWith("+json") || ct.endsWith("+xml")) return false;
  if (
    ct === "application/xml" ||
    ct === "application/javascript" ||
    ct === "application/x-www-form-urlencoded"
  ) {
    return false;
  }
  return true;
}

export function pickDownloadFilename(
  contentDisposition: string | null,
  contentType: string,
  targetPath: string,
): string {
  const fromHeader = parseContentDispositionFilename(contentDisposition);
  if (fromHeader) return fromHeader;
  const tail = targetPath.split("?")[0].split("/").filter(Boolean).pop() ?? "";
  const base = tail && /\.[A-Za-z0-9]{1,8}$/.test(tail) ? tail : "";
  if (base) return base;
  const ext = guessExtensionFromContentType(contentType);
  const stem = tail || "download";
  return ext ? `${stem}.${ext}` : `${stem}.bin`;
}

function parseContentDispositionFilename(disposition: string | null): string | null {
  if (!disposition) return null;
  const star = /filename\*\s*=\s*([^;]+)/i.exec(disposition);
  if (star) {
    const raw = star[1].trim();
    const m = /^[\w-]+'[^']*'(.+)$/.exec(raw);
    if (m) {
      try {
        return decodeURIComponent(m[1].replace(/^"|"$/g, ""));
      } catch {
        // fall through to plain
      }
    }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(disposition);
  return plain ? plain[1].trim() : null;
}

function guessExtensionFromContentType(contentType: string): string {
  const ct = contentType.split(";")[0].trim().toLowerCase();
  if (ct === "application/zip") return "zip";
  if (ct === "application/gzip" || ct === "application/x-gzip") return "gz";
  if (ct === "application/x-tar") return "tar";
  if (ct === "application/pdf") return "pdf";
  if (ct === "application/x-fasta" || ct === "application/fasta") return "fa";
  if (ct === "application/xml" || ct === "text/xml" || ct.endsWith("+xml")) return "xml";
  if (ct === "application/json" || ct === "application/problem+json" || ct.endsWith("+json"))
    return "json";
  if (ct === "text/html") return "html";
  if (ct === "text/csv") return "csv";
  if (ct === "text/tab-separated-values") return "tsv";
  if (ct === "text/plain") return "txt";
  return "";
}

export function formatBinarySummary(
  filename: string,
  bytes: number,
  contentType: string,
): string {
  const lines = [
    "// Binary response downloaded automatically.",
    `// file:         ${filename}`,
    `// size:         ${formatBytes(bytes)}`,
    `// content-type: ${contentType || "application/octet-stream"}`,
  ];
  return lines.join("\n");
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "unknown";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const decimals = unit === 0 ? 0 : value >= 100 ? 0 : value >= 10 ? 1 : 2;
  return `${value.toFixed(decimals)} ${units[unit]}`;
}

function triggerBrowserDownload(blob: Blob, filename: string): void {
  if (typeof document === "undefined" || typeof URL === "undefined") return;
  if (typeof URL.createObjectURL !== "function") return;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

function isAbortError(value: unknown): boolean {
  return value instanceof DOMException && value.name === "AbortError";
}
