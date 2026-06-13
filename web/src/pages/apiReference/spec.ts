import type { ParsedSpec, SpecEndpoint, SpecParam } from "@/pages/apiReference/types";

const CORE_NT_NC_003310_FASTA =
  [
    ">NC_003310.1:c48509-48048 Monkeypox virus, complete genome",
    "ATGGAGAAGCGAGAAGTTAATAAAGCTCTGTATGATCTTCAACGTAGTACTATGGTGTACAGTTCCGACG",
    "ATACTCCTCCTCGTTGGTCTACGACAATGGATGCTGATACACGGCCTACAGATTCTGATGCTGATGCTAT",
    "AATAGATGATGTATCCCGCGAAAAATCAATGAGAGAGGATAATAAGTCTTTTGATGATGTTATTCCGGTT",
    "AAAAAAATTATTTATTGGAAAGGTGTTAACCCTGTCACCGTTATTAATGAGTACTGCCAAATAACTAGGA",
    "GAGATTGGTCTTTTCGTATTGAATCAGTGGGGCCTAGTAACTCTCCTACATTTTATGCCTGTGTAGACAT",
    "TGACGGAAGAGTATTCGATAAGGCAGATGGAAAATCTAAACGAGATGCTAAAAATAATGCAGCTAAATTG",
    "GCTGTAGATAAACTTCTTAGTTATGTCATCATTAGATTCTGA",
  ].join("\n") + "\n";

const CORE_NT_BLAST_OPTIONS =
  "-word_size 28 -dust yes -soft_masking false -searchsp 32156241807668";

const CORE_NT_JOB_EXAMPLE = {
  summary: "Mode B - Web BLAST-equivalent core_nt",
  description:
    "Search core_nt with the same BLAST options used by New Search: blastn -db core_nt -evalue 0.05 -word_size 28 -max_target_seqs 100 -outfmt 5 -dust yes -soft_masking false -searchsp 32156241807668.",
  value: {
    program: "blastn",
    db: "core_nt",
    query_fasta: CORE_NT_NC_003310_FASTA,
    blast_options: {
      evalue: 0.05,
      max_target_seqs: 100,
      outfmt: "5",
      extra: CORE_NT_BLAST_OPTIONS,
    },
    resource_profile: "core_nt_safe",
  },
};

// E. coli K-12 MG1655 16S ribosomal RNA, partial (NCBI NR_024570.1, first ~490 bp).
// Used as the lightweight default `Try it` example so /v1/jobs can be exercised
// against the small 16S_ribosomal_RNA database (~50 MB) before core_nt is staged.
const SMALL_16S_RRNA_FASTA =
  [
    ">NR_024570.1 Escherichia coli str. K-12 substr. MG1655 16S ribosomal RNA, partial sequence",
    "AAATTGAAGAGTTTGATCATGGCTCAGATTGAACGCTGGCGGCAGGCCTAACACATGCAA",
    "GTCGAACGGTAACAGGAAGAAGCTTGCTTCTTTGCTGACGAGTGGCGGACGGGTGAGTAA",
    "TGTCTGGGAAACTGCCTGATGGAGGGGGATAACTACTGGAAACGGTAGCTAATACCGCAT",
    "AACGTCGCAAGACCAAAGAGGGGGACCTTCGGGCCTCTTGCCATCGGATGTGCCCAGATG",
    "GGATTAGCTAGTAGGTGGGGTAACGGCTCACCTAGGCGACGATCCCTAGCTGGTCTGAGA",
    "GGATGACCAGCCACACTGGAACTGAGACACGGTCCAGACTCCTACGGGAGGCAGCAGTGG",
    "GGAATATTGCACAATGGGCGCAAGCCTGATGCAGCCATGCCGCGTGTATGAAGAAGGCCT",
    "TCGGGTTGTAAAGTACTTTCAGCGGGGAGGAAGGGAGTAAAGTTAATACCTTTGCTCATT",
    "GACGTTACCCGCAGAAGAAGCACCGGCTAACTCCGTGCCAGCAGCCGCGGTAATACGGAG",
  ].join("\n") + "\n";

const SMALL_16S_RRNA_JOB_EXAMPLE = {
  summary: "Small - 16S ribosomal RNA (~50 MB DB)",
  description:
    "Lightweight default that runs against the small 16S_ribosomal_RNA database. Use this to exercise the API before staging core_nt (~700 MB). Query is E. coli K-12 16S rRNA partial (NR_024570.1).",
  value: {
    program: "blastn",
    db: "16S_ribosomal_RNA",
    query_fasta: SMALL_16S_RRNA_FASTA,
    blast_options: {
      evalue: 0.01,
      max_target_seqs: 50,
      outfmt: "5",
    },
    resource_profile: "standard",
  },
};

const CORE_NT_OUTFMT7_JOB_EXAMPLE = {
  summary: "Mode B - core_nt tabular (outfmt 7)",
  description:
    "Same Web BLAST-equivalent core_nt search as Mode B, but requests tabular output with comment lines (outfmt 7) instead of XML. outfmt 7 shares outfmt 6's 12-column data rows, so it runs sharded: the shard merge skips the per-shard comment headers and re-emits a single merged comment header. Use outfmt 7 when a downstream consumer wants tabular rows with the BLASTN / Query / Fields / hit-count comments.",
  value: {
    program: "blastn",
    db: "core_nt",
    query_fasta: CORE_NT_NC_003310_FASTA,
    blast_options: {
      evalue: 0.05,
      max_target_seqs: 100,
      outfmt: "7",
      extra: CORE_NT_BLAST_OPTIONS,
    },
    resource_profile: "core_nt_safe",
  },
};

const CORE_NT_OUTFMT7_TAXID_JOB_EXAMPLE = {
  summary: "Mode B - core_nt tabular + taxids (outfmt 7 std staxids)",
  description:
    "Adds taxonomy + strand + sequence columns to the tabular output via an extended outfmt specifier. The standard 12 columns MUST stay first (the `std` token), because the shard merge re-ranks by the fixed std positions (evalue=col11, bitscore=col12) and only then preserves the trailing columns; a non-std-leading order is rejected. The full specifier is passed as the `outfmt` value (the sibling keeps it verbatim) so the standard columns are not duplicated — do NOT also place -outfmt in `extra`. For Web BLAST-equivalent e-values and ranking, submit this from New Search with sharding_mode=precise (search-space correction + tie-order oracle). The multi-token outfmt is now verified end-to-end on a live sharded core_nt run (elb-openapi 4.22): each shard pod renders `-outfmt 7 std staxids sscinames` as a single blastn argument and the merged result carries the `subject tax ids` / `subject sci names` columns. If a shard fails to start, fall back to plain outfmt 7 (no extra columns) or outfmt 5 (XML).",
  value: {
    program: "blastn",
    db: "core_nt",
    query_fasta: CORE_NT_NC_003310_FASTA,
    blast_options: {
      evalue: 0.05,
      max_target_seqs: 100,
      outfmt: '7 std staxids sstrand qseq sseq',
      extra: CORE_NT_BLAST_OPTIONS,
    },
    resource_profile: "core_nt_safe",
  },
};

const OPENAPI_JOB_ID_DESCRIPTION =
  "Short OpenAPI job id returned by POST /v1/jobs, for example 17dfd2825089. Do not paste a Dashboard job UUID from /blast/jobs/<uuid>.";
const OPENAPI_JOB_ID_USAGE_HINT =
  "Use the short id returned as job_id / target.openapi_job_id. Dashboard UUIDs belong to /api/blast/jobs/{uuid} and /blast/jobs/<uuid>.";

type ResponseMap = NonNullable<SpecEndpoint["responses"]>;
type JsonSchema = {
  $ref?: string;
  type?: string;
  title?: string;
  format?: string;
  default?: unknown;
  example?: unknown;
  enum?: unknown[];
  properties?: Record<string, JsonSchema>;
  items?: JsonSchema;
  anyOf?: JsonSchema[];
  oneOf?: JsonSchema[];
  allOf?: JsonSchema[];
  additionalProperties?: unknown;
};
type JsonContent = {
  schema?: JsonSchema;
  example?: unknown;
  examples?: Record<string, { value?: unknown } | unknown>;
};
type RawResponse = ResponseMap[string] & {
  content?: Record<string, JsonContent>;
};

const REQUEST_ID_EXAMPLE = "01HX7V8W4D9Y3F9PZQ2QK4N7RA";
const OPENAPI_JOB_ID_EXAMPLE = "17dfd2825089";
const DASHBOARD_JOB_ID_EXAMPLE = "bb61858a-8cb6-4590-a2e3-c144662851f7";

const META_EXAMPLE = {
  request_id: REQUEST_ID_EXAMPLE,
};

const JOB_TARGET_EXAMPLE = {
  resource_type: "blast_job",
  job_id_kind: "openapi",
  openapi_job_id: OPENAPI_JOB_ID_EXAMPLE,
  dashboard_job_id: DASHBOARD_JOB_ID_EXAMPLE,
  links: {
    dashboard_status: `/api/blast/jobs/${DASHBOARD_JOB_ID_EXAMPLE}`,
    openapi_status: `/v1/jobs/${OPENAPI_JOB_ID_EXAMPLE}/status`,
  },
};

const JOB_ID_USAGE = [
  {
    label: "OpenAPI job id",
    value: OPENAPI_JOB_ID_EXAMPLE,
    useWith: `/v1/jobs/${OPENAPI_JOB_ID_EXAMPLE}/status`,
  },
  {
    label: "Dashboard job UUID",
    value: DASHBOARD_JOB_ID_EXAMPLE,
    useWith: `/blast/jobs/${DASHBOARD_JOB_ID_EXAMPLE} and /api/blast/jobs/${DASHBOARD_JOB_ID_EXAMPLE}`,
  },
];

const OPERATION_EXAMPLE = {
  operation_id: "task-123",
  operation_type: "blast.submit.openapi",
  state: "accepted",
  poll_after_seconds: 5,
  links: {
    self: "/api/operations/task-123",
    target: `/api/blast/jobs/${DASHBOARD_JOB_ID_EXAMPLE}`,
  },
};

function errorExample(
  code: string,
  message: string,
  extra?: Record<string, unknown>,
): Record<string, unknown> {
  const { error: extraError, ...extraRest } = extra || {};
  return {
    error: {
      code,
      message,
      ...((extraError as Record<string, unknown> | undefined) || {}),
    },
    ...extraRest,
    meta: META_EXAMPLE,
  };
}

const COMMON_RUNTIME_RESPONSES: ResponseMap = {
  "400": {
    description: "Request shape, parameter, or identifier is invalid.",
    shapeName: "InvalidRequest",
    nextAction: "Fix the request payload or path parameter before retrying.",
    fields: ["error.code", "error.message", "meta.request_id"],
    example: errorExample("invalid_request", "The request payload is invalid."),
  },
  "401": {
    description: "API token is missing, expired, or invalid.",
    shapeName: "Unauthorized",
    nextAction: "Copy a fresh API token from the token panel and retry.",
    fields: ["error.code", "error.message", "meta.request_id"],
    example: errorExample("unauthorized", "X-ELB-API-Token is missing or invalid."),
  },
  "422": {
    description: "Validation failed for one or more request fields.",
    shapeName: "ValidationError",
    nextAction: "Inspect error.details and correct the highlighted fields.",
    fields: ["error.code", "error.details", "meta.request_id"],
    example: errorExample("validation_error", "Request validation failed.", {
      error: {
        details: [{ field: "request.field", reason: "Value failed validation." }],
      },
    }),
  },
  "500": {
    description: "Control plane or ElasticBLAST runtime failed unexpectedly.",
    shapeName: "RuntimeFailure",
    nextAction: "Keep meta.request_id and retry after the runtime is healthy.",
    fields: ["error.code", "error.message", "meta.request_id"],
    example: errorExample(
      "runtime_failure",
      "The ElasticBLAST runtime failed unexpectedly.",
    ),
  },
};

const JOB_RESOURCE_RESPONSES: ResponseMap = {
  "404": {
    description: "The requested BLAST job does not exist or has expired.",
    shapeName: "JobNotFound",
    nextAction:
      "Verify that the path value is the short OpenAPI job id, not a Dashboard UUID.",
    fields: ["error.code", "error.message", "target", "meta.request_id"],
    idUsage: JOB_ID_USAGE,
    example: errorExample("job_not_found", "No BLAST job exists for this job_id.", {
      target: {
        resource_type: "blast_job",
        job_id_kind: "openapi",
        openapi_job_id: OPENAPI_JOB_ID_EXAMPLE,
      },
    }),
  },
};

const MUTATION_RESPONSES: ResponseMap = {
  "409": {
    description: "Current job or cluster state does not allow this operation.",
    shapeName: "Conflict",
    nextAction: "Refresh job status and retry only if the state becomes compatible.",
    fields: ["error.code", "target", "meta.request_id"],
    example: errorExample(
      "conflict",
      "The current job state does not allow this operation.",
      { target: JOB_TARGET_EXAMPLE },
    ),
  },
};

const ADMISSION_REJECTED_RESPONSE: ResponseMap = {
  "429": {
    description: "Queue depth or runtime capacity cannot accept more work yet.",
    shapeName: "AdmissionRejected",
    nextAction: "Wait for poll_after_seconds or reduce concurrent submissions.",
    fields: ["error.code", "admission.decision", "admission.reason"],
    example: errorExample("queue_saturated", "Submission queue is temporarily full.", {
      admission: {
        decision: "rejected",
        reason: "queue_saturated",
        queue: {
          state: "saturated",
          depth_bucket: "high",
          poll_after_seconds: 15,
        },
      },
    }),
  },
};

const SUBMIT_SUCCESS_RESPONSE: ResponseMap = {
  "202": {
    description: "Submission was accepted; final BLAST completion is still pending.",
    shapeName: "JobSubmitAccepted",
    nextAction: `Store the OpenAPI job id and poll /v1/jobs/${OPENAPI_JOB_ID_EXAMPLE}/status.`,
    fields: [
      "job_id (OpenAPI short id)",
      "status",
      "operation.links.self",
      "target.openapi_job_id",
      "admission.decision",
    ],
    idUsage: JOB_ID_USAGE,
    example: {
      job_id: OPENAPI_JOB_ID_EXAMPLE,
      status: "dispatching",
      operation_status_url: "/api/operations/task-123",
      operation: OPERATION_EXAMPLE,
      target: JOB_TARGET_EXAMPLE,
      admission: {
        decision: "accepted",
        reason: "queued_for_blast_execution",
        queue: {
          state: "accepted",
          depth_bucket: "unknown",
          poll_after_seconds: 5,
        },
      },
      meta: META_EXAMPLE,
    },
  },
};

const JOB_STATUS_SUCCESS_RESPONSE: ResponseMap = {
  "200": {
    description: "Current BLAST job lifecycle state.",
    shapeName: "JobStatus",
    nextAction:
      "Continue polling this endpoint with the same OpenAPI job id while status is dispatching, queued, or running.",
    fields: [
      "job_id (OpenAPI short id)",
      "status",
      "phase",
      "target.dashboard_job_id",
      "meta.request_id",
    ],
    idUsage: JOB_ID_USAGE,
    example: {
      job_id: OPENAPI_JOB_ID_EXAMPLE,
      status: "running",
      phase: "submitting",
      program: "blastn",
      db: "core_nt",
      target: JOB_TARGET_EXAMPLE,
      meta: META_EXAMPLE,
    },
  },
};

const JOB_LIST_SUCCESS_RESPONSE: ResponseMap = {
  "200": {
    description: "Paged list of BLAST jobs visible to the execution service.",
    shapeName: "JobList",
    nextAction: "Use each OpenAPI job id with /v1/jobs/{job_id}/status for details.",
    fields: ["jobs[].job_id (OpenAPI short id)", "jobs[].status", "count"],
    example: {
      jobs: [
        {
          job_id: OPENAPI_JOB_ID_EXAMPLE,
          status: "running",
          program: "blastn",
          db: "core_nt",
        },
      ],
      count: 1,
      meta: META_EXAMPLE,
    },
  },
};

const HEALTH_SUCCESS_RESPONSE: ResponseMap = {
  "200": {
    description: "Service is reachable and ready for API traffic.",
    shapeName: "HealthStatus",
    nextAction: "Use this as a readiness check before calling job endpoints.",
    fields: ["status", "service", "version"],
    example: {
      status: "ok",
      service: "elb-openapi",
      version: "1.0.0",
    },
  },
};

const CONFIG_SUCCESS_RESPONSE: ResponseMap = {
  "200": {
    description: "Runtime configuration exposed by the OpenAPI service.",
    shapeName: "RuntimeConfig",
    nextAction: "Confirm supported programs, databases, and result defaults.",
    fields: ["service", "defaults", "databases"],
    example: {
      service: "elb-openapi",
      defaults: {
        program: "blastn",
        db: "core_nt",
      },
      databases: ["core_nt"],
    },
  },
};

const CLUSTER_SUCCESS_RESPONSE: ResponseMap = {
  "200": {
    description: "AKS cluster nodes, pod phases, and status counts.",
    shapeName: "ClusterOverview",
    nextAction:
      "Use pod_summary and nodes to confirm runtime capacity before submitting or debugging jobs.",
    fields: [
      "cluster_name",
      "nodes[].name",
      "nodes[].status",
      "nodes[].instance_type",
      "pods[].name",
      "pods[].phase",
      "pod_summary",
    ],
    example: {
      cluster_name: "elb-cluster",
      nodes: [
        {
          name: "aks-blastpool-41800479-vmss00002p",
          status: "Ready",
          instance_type: "Standard_E16s_v5",
        },
      ],
      pods: [
        {
          name: "warm-core-nt-07-vs2rk",
          phase: "Succeeded",
          node: "aks-blastpool-41800479-vmss00002p",
        },
        {
          name: "blast-job-17dfd2825089-0",
          phase: "Running",
          node: "aks-blastpool-41800479-vmss00002q",
        },
      ],
      pod_summary: {
        Succeeded: 100,
        Running: 1,
      },
    },
  },
};

/** Header parameters that the dashboard attaches server-side and that
 *  external API consumers cannot supply themselves. We strip them from
 *  the parsed spec so they never appear in the API Reference UI
 *  (Parameters list, Try form, copy-as-curl) — they are an
 *  implementation detail of the dashboard ↔ elb-openapi trust channel,
 *  not part of the public contract. */
const HIDDEN_HEADER_PARAMS: ReadonlySet<string> = new Set(["x-elb-internal-token"]);

function isHiddenParam(param: SpecParam): boolean {
  if (param.in !== "header") return false;
  return HIDDEN_HEADER_PARAMS.has(String(param.name || "").toLowerCase());
}

function isOpenApiJobPath(path: string): boolean {
  return (
    path.startsWith("/v1/jobs/{job_id}") ||
    path.startsWith("/api/v1/elastic-blast/jobs/{job_id}")
  );
}

function withCuratedParameters(path: string, parameters: SpecParam[]): SpecParam[] {
  if (!isOpenApiJobPath(path)) return parameters;
  return parameters.map((param) => {
    if (param.in !== "path" || param.name !== "job_id") return param;
    return {
      ...param,
      description: OPENAPI_JOB_ID_DESCRIPTION,
      displayName: "OpenAPI job id",
      usageHint: OPENAPI_JOB_ID_USAGE_HINT,
      schema: {
        ...(param.schema || {}),
        default: OPENAPI_JOB_ID_EXAMPLE,
        pattern: "^[a-f0-9]{6,12}$",
      },
    };
  });
}

function withCuratedRequestExamples(
  path: string,
  method: string,
  requestBody: SpecEndpoint["requestBody"],
): SpecEndpoint["requestBody"] {
  if (path !== "/v1/jobs" || method !== "post") return requestBody;
  const jsonBody = requestBody?.content?.["application/json"];
  if (!jsonBody) return requestBody;

  return {
    ...requestBody,
    content: {
      ...requestBody.content,
      "application/json": {
        ...jsonBody,
        examples: {
          small_16s_rrna: SMALL_16S_RRNA_JOB_EXAMPLE,
          mode_b_core_nt: CORE_NT_JOB_EXAMPLE,
          mode_b_core_nt_outfmt7: CORE_NT_OUTFMT7_JOB_EXAMPLE,
          mode_b_core_nt_outfmt7_taxids: CORE_NT_OUTFMT7_TAXID_JOB_EXAMPLE,
          ...(jsonBody.examples || {}),
        },
      },
    },
  };
}

function withCuratedResponses(
  path: string,
  method: string,
  responses: SpecEndpoint["responses"],
  componentSchemas: Record<string, JsonSchema>,
): SpecEndpoint["responses"] {
  const source = normalizeResponses(path, method, responses || {}, componentSchemas);
  const isMutation = ["post", "put", "patch", "delete"].includes(method);
  const isSubmit = path === "/v1/jobs" && method === "post";
  const isJobList = path === "/v1/jobs" && method === "get";
  const jobScoped = isOpenApiJobPath(path);
  const isHealth = path === "/healthz" || path === "/v1/health";
  const isConfig = path === "/v1/config";
  const isClusterOverview = path === "/v1/cluster" && method === "get";

  if (isSubmit) {
    return mergeResponses(
      source,
      SUBMIT_SUCCESS_RESPONSE,
      COMMON_RUNTIME_RESPONSES,
      MUTATION_RESPONSES,
      ADMISSION_REJECTED_RESPONSE,
    );
  }

  if (jobScoped) {
    return mergeResponses(
      source,
      method === "get" ? JOB_STATUS_SUCCESS_RESPONSE : {},
      COMMON_RUNTIME_RESPONSES,
      JOB_RESOURCE_RESPONSES,
      isMutation ? MUTATION_RESPONSES : {},
    );
  }

  if (isJobList) {
    return mergeResponses(source, JOB_LIST_SUCCESS_RESPONSE, {
      "401": COMMON_RUNTIME_RESPONSES["401"],
      "500": COMMON_RUNTIME_RESPONSES["500"],
    });
  }

  if (isHealth) {
    return mergeResponses(source, HEALTH_SUCCESS_RESPONSE, {
      "500": COMMON_RUNTIME_RESPONSES["500"],
    });
  }

  if (isConfig) {
    return mergeResponses(source, CONFIG_SUCCESS_RESPONSE, {
      "401": COMMON_RUNTIME_RESPONSES["401"],
      "500": COMMON_RUNTIME_RESPONSES["500"],
    });
  }

  if (isClusterOverview) {
    return mergeResponses(
      source,
      CLUSTER_SUCCESS_RESPONSE,
      commonResponsesFor(source, ["401", "500"]),
    );
  }

  return mergeResponses(
    source,
    commonResponsesFor(source, ["401", "500"]),
    isMutation ? MUTATION_RESPONSES : {},
  );
}

function normalizeResponses(
  path: string,
  method: string,
  responses: ResponseMap,
  componentSchemas: Record<string, JsonSchema>,
): ResponseMap {
  return Object.fromEntries(
    Object.entries(responses).map(([code, response]) => {
      const raw = response as RawResponse;
      const schema = jsonSchemaFromResponse(raw);
      const fields = response.fields || fieldsFromSchema(schema, componentSchemas);
      const example =
        response.example ??
        exampleFromResponse(raw) ??
        exampleFromSchema(schema, componentSchemas);
      const hasDocumentedBody = Boolean(
        schema || fields.length > 0 || example !== undefined,
      );

      return [
        code,
        {
          ...response,
          description:
            response.description === "Successful Response" && !hasDocumentedBody
              ? "Success response; no schema published."
              : response.description,
          shapeName:
            response.shapeName || inferShapeName(path, method, code, response, schema),
          nextAction:
            response.nextAction ||
            (!hasDocumentedBody && code.startsWith("2")
              ? "Use Try it to inspect a live response because this endpoint does not publish a response body schema."
              : undefined),
          fields: fields.length > 0 ? fields : response.fields,
          example: example === undefined ? response.example : example,
        },
      ];
    }),
  );
}

function commonResponsesFor(source: ResponseMap, always: string[] = []): ResponseMap {
  const codes = new Set(always);
  for (const code of Object.keys(source)) {
    if (COMMON_RUNTIME_RESPONSES[code]) codes.add(code);
  }
  return Object.fromEntries(
    [...codes]
      .filter((code) => COMMON_RUNTIME_RESPONSES[code])
      .map((code) => [code, COMMON_RUNTIME_RESPONSES[code]]),
  );
}

function jsonSchemaFromResponse(response: RawResponse): JsonSchema | undefined {
  const content = response.content || {};
  const json = content["application/json"] || Object.values(content).find(Boolean);
  return json?.schema;
}

function exampleFromResponse(response: RawResponse): unknown {
  const content = response.content || {};
  const json = content["application/json"] || Object.values(content).find(Boolean);
  if (!json) return undefined;
  if (json.example !== undefined) return json.example;
  const firstExample = Object.values(json.examples || {})[0];
  if (firstExample && typeof firstExample === "object" && "value" in firstExample) {
    return firstExample.value;
  }
  return firstExample;
}

function inferShapeName(
  path: string,
  method: string,
  code: string,
  response: ResponseMap[string],
  schema?: JsonSchema,
): string {
  if (schema?.$ref) {
    const refName = schema.$ref.split("/").pop();
    if (refName) return refName;
  }
  const resolvedTitle = schema?.title;
  if (resolvedTitle && !resolvedTitle.startsWith("Response ")) return resolvedTitle;
  if (code === "422") return "ValidationError";
  if (code.startsWith("4")) return "ErrorResponse";
  if (code.startsWith("5")) return "RuntimeFailure";
  if (!code.startsWith("2")) return responseTitle(code, response.description);
  const nouns = path
    .split("/")
    .filter((part) => part && part !== "v1" && !part.startsWith("{"))
    .slice(-2);
  if (nouns.length === 0) return responseTitle(code, response.description);
  const suffix = method === "get" ? "Response" : "Result";
  return `${nouns.map(toPascalCase).join("")}${suffix}`;
}

function responseTitle(code: string, description?: string): string {
  if (description && description !== "Successful Response") return description;
  if (code.startsWith("2")) return "SuccessResponse";
  if (code.startsWith("4")) return "ErrorResponse";
  if (code.startsWith("5")) return "RuntimeFailure";
  return `HTTP${code}`;
}

function fieldsFromSchema(
  schema: JsonSchema | undefined,
  componentSchemas: Record<string, JsonSchema>,
): string[] {
  const resolved = resolveSchema(schema, componentSchemas);
  if (!resolved) return [];
  if (resolved.type === "array") {
    return fieldsFromSchema(resolved.items, componentSchemas)
      .slice(0, 8)
      .map((field) => `items[].${field}`);
  }
  const properties = resolved.properties || {};
  return Object.keys(properties).slice(0, 10);
}

function exampleFromSchema(
  schema: JsonSchema | undefined,
  componentSchemas: Record<string, JsonSchema>,
  depth = 0,
): unknown {
  if (!schema || depth > 4) return undefined;
  const resolved = resolveSchema(schema, componentSchemas);
  if (!resolved) return undefined;
  if (resolved.example !== undefined) return resolved.example;
  if (resolved.default !== undefined) return resolved.default;
  if (resolved.enum && resolved.enum.length > 0) return resolved.enum[0];
  const unionSchema = resolved.anyOf?.[0] || resolved.oneOf?.[0];
  if (unionSchema) return exampleFromSchema(unionSchema, componentSchemas, depth + 1);
  if (resolved.type === "array") {
    const item = exampleFromSchema(resolved.items, componentSchemas, depth + 1);
    return item === undefined ? [] : [item];
  }
  if (resolved.type === "object" || resolved.properties) {
    const entries = Object.entries(resolved.properties || {}).slice(0, 12);
    return Object.fromEntries(
      entries.map(([key, value]) => [
        key,
        exampleFromSchema(value, componentSchemas, depth + 1) ?? sampleForSchema(value),
      ]),
    );
  }
  return sampleForSchema(resolved);
}

function resolveSchema(
  schema: JsonSchema | undefined,
  componentSchemas: Record<string, JsonSchema>,
): JsonSchema | undefined {
  if (!schema) return undefined;
  if (schema.$ref) {
    const name = schema.$ref.replace("#/components/schemas/", "");
    return componentSchemas[name] || schema;
  }
  if (schema.allOf && schema.allOf.length > 0) {
    const merged = schema.allOf
      .map((item) => resolveSchema(item, componentSchemas))
      .filter(Boolean) as JsonSchema[];
    return {
      ...schema,
      properties: Object.assign({}, ...merged.map((item) => item.properties || {})),
    };
  }
  return schema;
}

function sampleForSchema(schema: JsonSchema | undefined): unknown {
  const resolved = schema || {};
  if (resolved.type === "integer" || resolved.type === "number") return 0;
  if (resolved.type === "boolean") return true;
  if (resolved.type === "array") return [];
  if (resolved.type === "object") return {};
  if (resolved.format === "date-time") return "2026-05-21T00:00:00Z";
  return "string";
}

function toPascalCase(value: string): string {
  return value
    .replace(/[^a-zA-Z0-9]+/g, " ")
    .split(" ")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join("");
}

function mergeResponses(...maps: ResponseMap[]): ResponseMap {
  return maps.reduce<ResponseMap>((merged, current) => {
    for (const [code, response] of Object.entries(current)) {
      merged[code] = {
        ...(merged[code] || {}),
        ...response,
      };
    }
    return merged;
  }, {});
}

export function getDefaultRequestExampleKey(
  endpoint: Pick<SpecEndpoint, "path" | "method">,
  exampleKeys: string[],
): string {
  if (endpoint.path === "/v1/jobs" && endpoint.method === "post") {
    // Default to the lightweight 16S example so the dashboard's Try it surface
    // works against a small staged database before core_nt is ready. The
    // Web BLAST-equivalent core_nt entry stays available in the dropdown.
    if (exampleKeys.includes("small_16s_rrna")) return "small_16s_rrna";
    if (exampleKeys.includes("mode_b_core_nt")) return "mode_b_core_nt";
    return (
      exampleKeys.find((key) => key.toLowerCase().includes("mode_b")) ||
      exampleKeys[0] ||
      ""
    );
  }

  return exampleKeys[0] || "";
}

export function parseSpec(raw: Record<string, unknown>, baseUrl: string): ParsedSpec {
  const info = (raw.info || {}) as Record<string, string>;
  const tags = (raw.tags || []) as { name: string; description?: string }[];
  const componentSchemas =
    (raw.components as { schemas?: Record<string, JsonSchema> } | undefined)?.schemas ||
    {};
  const paths = (raw.paths || {}) as Record<
    string,
    Record<string, Record<string, unknown>>
  >;
  const endpoints: SpecEndpoint[] = [];

  for (const [path, methods] of Object.entries(paths)) {
    for (const [method, detail] of Object.entries(methods)) {
      if (!["get", "post", "put", "delete", "patch"].includes(method)) continue;
      const rawParams = (detail.parameters as SpecParam[]) || [];
      endpoints.push({
        method,
        path,
        summary: detail.summary as string | undefined,
        description: detail.description as string | undefined,
        tags: (detail.tags as string[]) || [],
        parameters: withCuratedParameters(
          path,
          rawParams.filter((param) => !isHiddenParam(param)),
        ),
        requestBody: withCuratedRequestExamples(
          path,
          method,
          detail.requestBody as SpecEndpoint["requestBody"],
        ),
        responses: withCuratedResponses(
          path,
          method,
          detail.responses as SpecEndpoint["responses"],
          componentSchemas,
        ),
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
