import { describe, expect, it } from "vitest";

import {
  buildCurl,
  buildTargetPath,
  formatBinarySummary,
  formatResponseBody,
  isBinaryContentType,
  pickDownloadFilename,
} from "./useOpenApiExecutor";

describe("OpenAPI executor path builder", () => {
  it("encodes path parameters before sending them through the proxy", () => {
    expect(
      buildTargetPath(
        "/v1/jobs/{job_id}/files/{file_id}",
        [
          { name: "job_id", in: "path" },
          { name: "file_id", in: "path" },
        ],
        { job_id: "job/with/slash", file_id: "result 001" },
      ),
    ).toBe("/v1/jobs/job%2Fwith%2Fslash/files/result%20001");
  });

  it("appends non-empty query parameters", () => {
    expect(
      buildTargetPath(
        "/v1/jobs",
        [
          { name: "status", in: "query" },
          { name: "empty", in: "query" },
          { name: "limit", in: "query" },
        ],
        { status: "running now", empty: "", limit: "10" },
      ),
    ).toBe("/v1/jobs?status=running+now&limit=10");
  });
});

describe("OpenAPI executor binary handling", () => {
  it("flags application/zip and application/octet-stream as binary", () => {
    expect(isBinaryContentType("application/zip")).toBe(true);
    expect(isBinaryContentType("application/octet-stream; charset=binary")).toBe(true);
    expect(isBinaryContentType("application/gzip")).toBe(true);
  });

  it("keeps json and text content-types as inline", () => {
    expect(isBinaryContentType("application/json")).toBe(false);
    expect(isBinaryContentType("application/problem+json")).toBe(false);
    expect(isBinaryContentType("text/plain; charset=utf-8")).toBe(false);
    expect(isBinaryContentType("application/xml")).toBe(false);
  });

  it("prefers filename from Content-Disposition when present", () => {
    expect(
      pickDownloadFilename(
        'attachment; filename="merged_results.zip"',
        "application/zip",
        "/v1/jobs/abc/results",
      ),
    ).toBe("merged_results.zip");
  });

  it("decodes RFC 5987 filename* parameter", () => {
    expect(
      pickDownloadFilename(
        "attachment; filename*=UTF-8''job%20alpha.zip",
        "application/zip",
        "/v1/jobs/abc/results",
      ),
    ).toBe("job alpha.zip");
  });

  it("falls back to content-type extension when header is missing", () => {
    expect(pickDownloadFilename(null, "application/zip", "/v1/jobs/abc/results")).toBe(
      "results.zip",
    );
    expect(
      pickDownloadFilename(null, "application/octet-stream", "/v1/jobs/abc/results"),
    ).toBe("results.bin");
  });

  it("derives a textual extension for inline XML / JSON results", () => {
    expect(pickDownloadFilename(null, "application/xml", "/v1/jobs/abc/results")).toBe(
      "results.xml",
    );
    expect(
      pickDownloadFilename(null, "application/json; charset=utf-8", "/v1/jobs/abc/results"),
    ).toBe("results.json");
    expect(pickDownloadFilename(null, "text/plain", "/v1/jobs/abc/results")).toBe(
      "results.txt",
    );
  });

  it("formats a readable summary for binary downloads", () => {
    const text = formatBinarySummary("merged_results.zip", 2048, "application/zip");
    expect(text).toContain("merged_results.zip");
    expect(text).toContain("2.00 KiB");
    expect(text).toContain("application/zip");
  });
});

describe("OpenAPI executor response body formatter", () => {
  it("shows an explicit notice when a textual response has an empty body", () => {
    expect(formatResponseBody("")).toBe(
      "(empty response body — the server returned 0 bytes)",
    );
    expect(formatResponseBody("   \n  ")).toBe(
      "(empty response body — the server returned 0 bytes)",
    );
  });

  it("pretty-prints JSON and passes other text through unchanged", () => {
    expect(formatResponseBody('{"a":1}')).toBe('{\n  "a": 1\n}');
    expect(formatResponseBody("<root>x</root>")).toBe("<root>x</root>");
  });
});

describe("OpenAPI executor curl builder", () => {
  it("builds a direct curl for non-proxy endpoints without an Authorization header", () => {
    const curl = buildCurl({
      endpoint: {
        method: "get",
        path: "/healthz",
        parameters: [],
      },
      baseUrl: "https://elb-openapi.example",
      proxyInfo: undefined,
      paramValues: {},
      bodyText: "",
      apiBase: "",
      origin: "https://dash.example",
    });
    expect(curl).toContain("curl -X GET 'https://elb-openapi.example/healthz'");
    expect(curl).not.toContain("Authorization");
    expect(curl).not.toContain("--data-raw");
  });

  it("builds a proxy curl with an X-ELB-API-Token placeholder (M2M)", () => {
    const curl = buildCurl({
      endpoint: {
        method: "post",
        path: "/v1/jobs/{job_id}/cancel",
        parameters: [{ name: "job_id", in: "path" }],
        requestBody: { content: { "application/json": {} } },
      },
      baseUrl: "",
      proxyInfo: { sub: "sub-1", rg: "rg-1", clusterName: "aks-1" },
      paramValues: { job_id: "abc/123" },
      bodyText: '{"reason":"user-cancel"}',
      apiBase: "",
      origin: "https://dash.example",
    });
    expect(curl).toContain("curl -X POST");
    expect(curl).toContain("https://dash.example/api/aks/openapi/proxy?");
    expect(curl).toContain("path=%2Fv1%2Fjobs%2Fabc%252F123%2Fcancel");
    expect(curl).toContain("'X-ELB-API-Token: $ELB_API_TOKEN'");
    // Never emit a Bearer header — the copy-curl surface is M2M-shaped so a
    // copied command works on a peer VM without a live MSAL token.
    expect(curl).not.toContain("Authorization");
    expect(curl).not.toContain("$AAD_TOKEN");
    expect(curl).toContain("'Content-Type: application/json'");
    expect(curl).toContain(`--data-raw '{"reason":"user-cancel"}'`);
  });

  it("emits the X-ELB-API-Token placeholder regardless of proxy vs dashboardApi", () => {
    for (const cfg of [
      { proxyInfo: { sub: "s", rg: "r", clusterName: "c" }, dashboardApi: false },
      { proxyInfo: undefined, dashboardApi: true },
    ]) {
      const curl = buildCurl({
        endpoint: { method: "get", path: "/v1/jobs", parameters: [] },
        baseUrl: "",
        proxyInfo: cfg.proxyInfo,
        dashboardApi: cfg.dashboardApi,
        paramValues: {},
        bodyText: "",
        apiBase: "",
        origin: "https://dash.example",
      });
      expect(curl).toContain("'X-ELB-API-Token: $ELB_API_TOKEN'");
      expect(curl).not.toContain("Authorization");
    }
  });

  it("inlines the real M2M token value when provided", () => {
    const curl = buildCurl({
      endpoint: { method: "get", path: "/v1/jobs", parameters: [] },
      baseUrl: "",
      dashboardApi: true,
      paramValues: {},
      bodyText: "",
      apiBase: "",
      origin: "https://dash.example",
      m2mToken: "xTmEoreeMkkTzNqwLNZLFYglstWPQn29M44sBtmPm4Q",
    });
    expect(curl).toContain(
      "'X-ELB-API-Token: xTmEoreeMkkTzNqwLNZLFYglstWPQn29M44sBtmPm4Q'",
    );
    // Real value replaces the placeholder — do NOT leak $ELB_API_TOKEN
    // once the SPA has fetched the shared token.
    expect(curl).not.toContain("$ELB_API_TOKEN");
    expect(curl).not.toContain("Authorization");
  });

  it("falls back to the placeholder when m2mToken is undefined or empty", () => {
    for (const tok of [undefined, ""]) {
      const curl = buildCurl({
        endpoint: { method: "get", path: "/v1/jobs", parameters: [] },
        baseUrl: "",
        proxyInfo: { sub: "s", rg: "r", clusterName: "c" },
        paramValues: {},
        bodyText: "",
        apiBase: "",
        origin: "https://dash.example",
        m2mToken: tok,
      });
      expect(curl).toContain("'X-ELB-API-Token: $ELB_API_TOKEN'");
    }
  });

  it("escapes single quotes in body so the command stays POSIX-safe", () => {
    const curl = buildCurl({
      endpoint: {
        method: "post",
        path: "/v1/echo",
        parameters: [],
        requestBody: { content: { "application/json": {} } },
      },
      baseUrl: "https://api.example",
      proxyInfo: undefined,
      paramValues: {},
      bodyText: "it's fine",
      apiBase: "",
      origin: "",
    });
    expect(curl).toContain(`--data-raw 'it'\\''s fine'`);
  });

  it("prefers apiBase over origin when provided (local dev: VITE_API_BASE_URL=http://localhost:8085)", () => {
    const curl = buildCurl({
      endpoint: { method: "get", path: "/v1/x", parameters: [] },
      baseUrl: "",
      proxyInfo: { sub: "s", rg: "r", clusterName: "c" },
      paramValues: {},
      bodyText: "",
      apiBase: "http://localhost:8085",
      origin: "http://localhost:8090",
    });
    expect(curl).toContain("http://localhost:8085/api/aks/openapi/proxy?");
    expect(curl).not.toContain("http://localhost:8090/api/aks/openapi/proxy?");
  });

  it("builds a same-origin curl with the X-ELB-API-Token header in dashboardApi mode", () => {
    const curl = buildCurl({
      endpoint: {
        method: "post",
        path: "/api/aks/openapi/ensure-running",
        parameters: [],
        requestBody: { content: { "application/json": {} } },
      },
      baseUrl: "",
      dashboardApi: true,
      paramValues: {},
      bodyText: '{"resource_group":"rg","cluster_name":"c"}',
      apiBase: "",
      origin: "https://dash.example",
    });
    // Same-origin dashboard host (NOT the elb-openapi proxy path).
    expect(curl).toContain(
      "curl -X POST 'https://dash.example/api/aks/openapi/ensure-running'",
    );
    expect(curl).not.toContain("/api/aks/openapi/proxy");
    expect(curl).toContain("'X-ELB-API-Token: $ELB_API_TOKEN'");
    expect(curl).not.toContain("Authorization");
    expect(curl).toContain(
      `--data-raw '{"resource_group":"rg","cluster_name":"c"}'`,
    );
  });
});

