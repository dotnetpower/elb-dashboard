import { describe, expect, it } from "vitest";

import {
  buildTargetPath,
  formatBinarySummary,
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

  it("formats a readable summary for binary downloads", () => {
    const text = formatBinarySummary("merged_results.zip", 2048, "application/zip");
    expect(text).toContain("merged_results.zip");
    expect(text).toContain("2.00 KiB");
    expect(text).toContain("application/zip");
  });
});
