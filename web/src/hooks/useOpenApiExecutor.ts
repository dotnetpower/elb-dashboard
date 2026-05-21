import { useCallback, useEffect, useRef, useState } from "react";

import { fetchApiRawNoRedirect } from "@/api/client";
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
      if (isCurrent()) {
        setResponse({
          status: resp.status,
          body: rendered.body,
          time: Date.now() - start,
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

  return { execute, response, loading, copyResponse };
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

function formatResponseBody(text: string): string {
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
  download?: { filename: string; bytes: number; contentType: string };
}> {
  const contentType = resp.headers.get("content-type") ?? "";
  if (resp.ok && isBinaryContentType(contentType)) {
    const blob = await resp.blob();
    const filename = pickDownloadFilename(
      resp.headers.get("content-disposition"),
      contentType,
      targetPath,
    );
    triggerBrowserDownload(blob, filename);
    return {
      body: formatBinarySummary(filename, blob.size, contentType || blob.type),
      download: {
        filename,
        bytes: blob.size,
        contentType: contentType || blob.type || "application/octet-stream",
      },
    };
  }
  const text = await resp.text();
  return { body: formatResponseBody(text) };
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
