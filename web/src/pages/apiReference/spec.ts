import type { ParsedSpec, SpecEndpoint, SpecParam } from "@/pages/apiReference/types";

export function parseSpec(raw: Record<string, unknown>, baseUrl: string): ParsedSpec {
  const info = (raw.info || {}) as Record<string, string>;
  const tags = (raw.tags || []) as { name: string; description?: string }[];
  const paths = (raw.paths || {}) as Record<
    string,
    Record<string, Record<string, unknown>>
  >;
  const endpoints: SpecEndpoint[] = [];

  for (const [path, methods] of Object.entries(paths)) {
    for (const [method, detail] of Object.entries(methods)) {
      if (!["get", "post", "put", "delete", "patch"].includes(method)) continue;
      endpoints.push({
        method,
        path,
        summary: detail.summary as string | undefined,
        description: detail.description as string | undefined,
        tags: (detail.tags as string[]) || [],
        parameters: (detail.parameters as SpecParam[]) || [],
        requestBody: detail.requestBody as SpecEndpoint["requestBody"],
        responses: detail.responses as SpecEndpoint["responses"],
      });
    }
  }

  return {
    title: info.title || "API",
    version: info.version || "",
    description: info.description || "",
    tags,
    endpoints,
    baseUrl,
  };
}

export function isSimpleEndpoint(ep: SpecEndpoint): boolean {
  const hasRequiredPathParams = ep.parameters.some((p) => p.in === "path" && p.required);
  return ep.method === "get" && !hasRequiredPathParams && !ep.requestBody;
}

export function statusColor(code: number): string {
  if (code >= 200 && code < 300) return "var(--success)";
  if (code >= 400 && code < 500) return "var(--warning)";
  if (code >= 500) return "var(--danger)";
  return "var(--text-muted)";
}