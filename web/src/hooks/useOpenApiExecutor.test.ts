import { describe, expect, it } from "vitest";

import { buildTargetPath } from "./useOpenApiExecutor";

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