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
        ? await executeViaProxy(endpoint, proxyInfo, targetPath, bodyText, controller.signal)
        : await executeDirect(endpoint, baseUrl, targetPath, bodyText, controller.signal);
      const text = await resp.text();
      if (isCurrent()) {
        setResponse({ status: resp.status, body: formatResponseBody(text), time: Date.now() - start });
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

function isAbortError(value: unknown): boolean {
  return value instanceof DOMException && value.name === "AbortError";
}