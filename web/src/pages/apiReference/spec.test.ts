import { describe, expect, it } from "vitest";

import { getDefaultRequestExampleKey, parseSpec } from "./spec";

describe("API Reference spec parser", () => {
  it("prefers the curated core_nt inline FASTA example for POST /v1/jobs", () => {
    const parsed = parseSpec(
      {
        info: { title: "ElasticBLAST API", version: "1" },
        paths: {
          "/v1/jobs": {
            post: {
              tags: ["Jobs"],
              requestBody: {
                content: {
                  "application/json": {
                    examples: {
                      mode_a: {
                        summary: "Mode A",
                        value: { program: "blastn", db: "https://example/db" },
                      },
                    },
                  },
                },
              },
            },
          },
        },
      },
      "https://api.example.internal",
    );

    const endpoint = parsed.endpoints[0];
    const examples = endpoint.requestBody?.content?.["application/json"]?.examples || {};
    const firstKey = Object.keys(examples)[0];
    const firstExample = examples[firstKey].value as {
      db: string;
      query_fasta: string;
      blast_options: {
        evalue: number;
        max_target_seqs: number;
        outfmt: string;
        extra: string;
      };
      resource_profile: string;
    };

    expect(firstKey).toBe("mode_b_core_nt");
    expect(firstExample.db).toBe("core_nt");
    expect(firstExample.query_fasta).toContain(">NC_003310.1:c48509-48048");
    expect(firstExample.query_fasta).toContain("\nATGGAGAAGCGAGAAGTTAA");
    expect(firstExample.blast_options).toEqual({
      evalue: 0.05,
      max_target_seqs: 100,
      outfmt: "5",
      extra: "-word_size 28 -dust yes -soft_masking false -searchsp 32156241807668",
    });
    expect(firstExample.resource_profile).toBe("core_nt_safe");
  });

  it("selects Mode B as the default request body example for POST /v1/jobs", () => {
    expect(
      getDefaultRequestExampleKey({ path: "/v1/jobs", method: "post" }, [
        "mode_a",
        "mode_b",
        "mode_b_taxid",
      ]),
    ).toBe("mode_b");

    expect(
      getDefaultRequestExampleKey({ path: "/v1/jobs", method: "post" }, [
        "mode_a",
        "mode_b_core_nt",
        "mode_b_taxid",
      ]),
    ).toBe("mode_b_core_nt");
  });

  it("adds response shapes and examples to submit and job status endpoints", () => {
    const parsed = parseSpec(
      {
        info: { title: "ElasticBLAST API", version: "1" },
        paths: {
          "/v1/jobs": {
            post: {
              tags: ["Jobs"],
              responses: {
                "202": { description: "Successful Response" },
                "422": { description: "Validation Error" },
              },
            },
          },
          "/v1/jobs/{job_id}/status": {
            get: {
              tags: ["Jobs"],
              parameters: [
                {
                  name: "job_id",
                  in: "path",
                  required: true,
                  schema: { type: "string" },
                },
              ],
              responses: {
                "200": { description: "Successful Response" },
                "422": { description: "Validation Error" },
              },
            },
          },
        },
      },
      "https://api.example.internal",
    );

    const submit = parsed.endpoints.find(
      (endpoint) => endpoint.path === "/v1/jobs" && endpoint.method === "post",
    );
    const status = parsed.endpoints.find(
      (endpoint) =>
        endpoint.path === "/v1/jobs/{job_id}/status" && endpoint.method === "get",
    );

    expect(Object.keys(submit?.responses || {}).sort()).toEqual([
      "202",
      "400",
      "401",
      "409",
      "422",
      "429",
      "500",
    ]);
    expect(submit?.responses?.["202"]?.shapeName).toBe("JobSubmitAccepted");
    expect(submit?.responses?.["202"]?.nextAction).toContain("poll");
    expect(submit?.responses?.["202"]?.fields).toContain("job_id (OpenAPI short id)");
    expect(submit?.responses?.["202"]?.idUsage).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          label: "OpenAPI job id",
          value: "17dfd2825089",
          useWith: "/v1/jobs/17dfd2825089/status",
        }),
        expect.objectContaining({
          label: "Dashboard job UUID",
          value: "bb61858a-8cb6-4590-a2e3-c144662851f7",
        }),
      ]),
    );
    expect(submit?.responses?.["202"]?.example).toMatchObject({
      job_id: "17dfd2825089",
      operation: { operation_id: "task-123" },
      admission: { decision: "accepted" },
    });
    expect(submit?.responses?.["429"]?.shapeName).toBe("AdmissionRejected");
    expect(submit?.responses?.["429"]?.example).toMatchObject({
      admission: { decision: "rejected", reason: "queue_saturated" },
    });

    expect(Object.keys(status?.responses || {}).sort()).toEqual([
      "200",
      "400",
      "401",
      "404",
      "422",
      "500",
    ]);
    expect(status?.parameters[0]).toMatchObject({
      name: "job_id",
      displayName: "OpenAPI job id",
      usageHint: expect.stringContaining("Dashboard UUIDs belong"),
      schema: { default: "17dfd2825089", pattern: "^[a-f0-9]{6,12}$" },
    });
    expect(status?.responses?.["200"]?.shapeName).toBe("JobStatus");
    expect(status?.responses?.["200"]?.fields).toContain("job_id (OpenAPI short id)");
    expect(status?.responses?.["200"]?.idUsage?.[0]).toMatchObject({
      label: "OpenAPI job id",
      useWith: "/v1/jobs/17dfd2825089/status",
    });
    expect(status?.responses?.["404"]?.shapeName).toBe("JobNotFound");
  });

  it("adds concrete 200 response examples for cluster overview", () => {
    const parsed = parseSpec(
      {
        info: { title: "ElasticBLAST API", version: "1" },
        paths: {
          "/v1/cluster": {
            get: {
              tags: ["Cluster"],
              summary: "Cluster overview",
              responses: {
                "200": { description: "Successful Response" },
                "422": { description: "Validation Error" },
              },
            },
          },
        },
      },
      "https://api.example.internal",
    );

    const endpoint = parsed.endpoints[0];

    expect(endpoint.responses?.["200"]?.shapeName).toBe("ClusterOverview");
    expect(endpoint.responses?.["200"]?.fields).toEqual(
      expect.arrayContaining(["nodes[].status", "pods[].phase", "pod_summary"]),
    );
    expect(endpoint.responses?.["200"]?.example).toMatchObject({
      cluster_name: "elb-cluster",
      nodes: [{ status: "Ready", instance_type: "Standard_E16s_v5" }],
      pod_summary: { Succeeded: 100, Running: 1 },
    });
    expect(endpoint.responses?.["422"]?.shapeName).toBe("ValidationError");
    expect(endpoint.responses?.["422"]?.example).toMatchObject({
      error: { code: "validation_error" },
    });
  });

  it("derives fields and JSON examples from OpenAPI response schemas", () => {
    const parsed = parseSpec(
      {
        info: { title: "ElasticBLAST API", version: "1" },
        components: {
          schemas: {
            RuntimeInfo: {
              type: "object",
              properties: {
                service: { type: "string", example: "elb-openapi" },
                ready: { type: "boolean" },
                workers: { type: "integer" },
              },
            },
          },
        },
        paths: {
          "/v1/runtime": {
            get: {
              tags: ["System"],
              responses: {
                "200": {
                  description: "Runtime details",
                  content: {
                    "application/json": {
                      schema: { $ref: "#/components/schemas/RuntimeInfo" },
                    },
                  },
                },
              },
            },
          },
          "/v1/undocumented": {
            get: {
              tags: ["System"],
              responses: {
                "200": { description: "Successful Response" },
              },
            },
          },
        },
      },
      "https://api.example.internal",
    );

    const runtime = parsed.endpoints.find((endpoint) => endpoint.path === "/v1/runtime");
    const undocumented = parsed.endpoints.find(
      (endpoint) => endpoint.path === "/v1/undocumented",
    );

    expect(runtime?.responses?.["200"]?.shapeName).toBe("RuntimeInfo");
    expect(runtime?.responses?.["200"]?.fields).toEqual(["service", "ready", "workers"]);
    expect(runtime?.responses?.["200"]?.example).toEqual({
      service: "elb-openapi",
      ready: true,
      workers: 0,
    });

    expect(undocumented?.responses?.["200"]?.description).toBe(
      "Success response; no schema published.",
    );
    expect(undocumented?.responses?.["200"]?.nextAction).toContain("Try it");
  });
});
