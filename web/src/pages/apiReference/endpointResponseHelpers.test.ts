import { describe, expect, it } from "vitest";

import {
  getPathIdHint,
  responseBackground,
  responseBorder,
  responseTitle,
  responseTone,
  safeParseJson,
  sortResponses,
  type ResponseEntry,
} from "./endpointResponseHelpers";

describe("endpointResponseHelpers", () => {
  it("safeParseJson returns null on empty / invalid, parses valid", () => {
    expect(safeParseJson("")).toBeNull();
    expect(safeParseJson("{not json")).toBeNull();
    expect(safeParseJson('{"a":1}')).toEqual({ a: 1 });
  });

  it("sortResponses orders numeric codes ascending, non-numeric last", () => {
    const entries: ResponseEntry[] = [
      ["500", {}],
      ["default", {}],
      ["200", {}],
      ["404", {}],
    ];
    expect(sortResponses(entries).map(([c]) => c)).toEqual([
      "200",
      "404",
      "500",
      "default",
    ]);
  });

  it("sortResponses does not mutate the input", () => {
    const entries: ResponseEntry[] = [
      ["500", {}],
      ["200", {}],
    ];
    const before = entries.map(([c]) => c);
    sortResponses(entries);
    expect(entries.map(([c]) => c)).toEqual(before);
  });

  it("getPathIdHint only fires for {job_id} paths", () => {
    expect(getPathIdHint("/v1/jobs")).toBeUndefined();
    expect(getPathIdHint("/v1/jobs/{job_id}")?.label).toBe("job_id = OpenAPI id");
  });

  it("responseTitle prefers a meaningful description, else derives from code", () => {
    expect(responseTitle("200", "Custom OK")).toBe("Custom OK");
    expect(responseTitle("200", "Successful Response")).toBe("SuccessResponse");
    expect(responseTitle("404")).toBe("ErrorResponse");
    expect(responseTitle("500")).toBe("RuntimeFailure");
    expect(responseTitle("301")).toBe("HTTP301");
  });

  it("response colour helpers map success / warning / danger / default", () => {
    expect(responseTone("200")).toContain("success");
    expect(responseTone("429")).toContain("warning");
    expect(responseTone("500")).toContain("danger");
    expect(responseTone("301")).toContain("faint");
    expect(responseBackground("409")).toContain("245,166,35");
    expect(responseBorder("204")).toContain("115,191,105");
  });
});
