/**
 * platform-flows.api.spec.ts — integrated platform E2E (api-smoke lane).
 *
 * Covers the full control-plane flow the maintainer asked to validate, organised
 * scenario-by-scenario so each `test` runs standalone (Playwright `--grep`) and
 * the whole file runs as one integrated pass (it is part of `e2e:all-safe` via
 * the `*.api.spec.ts` → api-smoke project match).
 *
 * Cost model:
 *   - DEFAULT (cost 0): read-only contract checks against the real local API
 *     (dev-bypass). Routes are designed to degrade (never 5xx) when Azure
 *     credentials / live resources are absent, so these assert the response
 *     SHAPE and that auth is wired, not live Azure state.
 *   - GATED (real Azure side effects): cluster stop/start, auto-stop extend,
 *     Service Bus enqueue, and real BLAST submit are each behind an explicit
 *     env flag and `test.skip` so a normal run never spends money or mutates a
 *     live cluster. Flip the flag (and provide the E2E_AZURE_* targets) to run
 *     the live tier.
 *
 * Persona / permission coverage is asserted exhaustively by the backend
 * `api/tests/test_persona_matrix.py` (owner / contributor / reader / dev-bypass)
 * because the api-smoke lane runs under a single dev-bypass identity and cannot
 * mint the four real tokens; this spec only confirms the dev-bypass identity is
 * the one the API sees.
 */
import { expect, test, type APIRequestContext, type APIResponse } from "@playwright/test";

const apiUrl = process.env.E2E_API_URL ?? "http://127.0.0.1:8085";
const bearerToken = process.env.E2E_BEARER_TOKEN ?? "";

const allowBlastSubmit = process.env.E2E_ALLOW_BLAST_SUBMIT === "1";
const allowAksPower = process.env.E2E_ALLOW_AKS_POWER === "1";
const allowAutostopMutate = process.env.E2E_ALLOW_AKS_AUTOSTOP_MUTATE === "1";
const allowSbSend = process.env.E2E_ALLOW_SB_SEND === "1";

const target = {
  subscriptionId:
    process.env.E2E_AZURE_SUBSCRIPTION_ID ?? "00000000-0000-0000-0000-000000000000",
  resourceGroup: process.env.E2E_AZURE_RESOURCE_GROUP ?? "rg-elb-e2e",
  clusterName: process.env.E2E_AKS_CLUSTER ?? "aks-e2e",
  storageAccount: process.env.E2E_STORAGE_ACCOUNT ?? "stelbe2e",
  region: process.env.E2E_AZURE_REGION ?? "koreacentral",
};

function authHeaders(): Record<string, string> | undefined {
  return bearerToken ? { Authorization: `Bearer ${bearerToken}` } : undefined;
}

async function getJson(
  request: APIRequestContext,
  path: string,
  timeoutMs?: number,
): Promise<APIResponse> {
  return request.get(`${apiUrl}${path}`, { headers: authHeaders(), timeout: timeoutMs });
}

async function postJson(
  request: APIRequestContext,
  path: string,
  data: unknown,
  timeoutMs?: number,
): Promise<APIResponse> {
  return request.post(`${apiUrl}${path}`, { data, headers: authHeaders(), timeout: timeoutMs });
}

/** Read routes must never 401 (dev-bypass) and never 5xx (graceful degrade). */
function expectHealthyRead(response: APIResponse, body: string): void {
  expect(response.status(), `expected non-401 (auth wired): ${body}`).not.toBe(401);
  expect(response.status(), `expected non-5xx (graceful degrade): ${body}`).toBeLessThan(500);
}

/**
 * Reachable + sanitised: never 401 (auth wired); a 2xx/4xx is fine, and a 5xx is
 * only acceptable when it is a STRUCTURED degrade (carries a ``code``) rather
 * than an unhandled crash. Used for routes that legitimately return 503 with a
 * code when an optional plane (OpenAPI / Service Bus) is not configured locally.
 */
async function expectReachable(response: APIResponse): Promise<void> {
  const text = await response.text();
  expect(response.status(), `expected non-401 (auth wired): ${text}`).not.toBe(401);
  if (response.status() >= 500) {
    let code = "";
    try {
      code = String(JSON.parse(text).code ?? JSON.parse(text).detail?.code ?? "");
    } catch {
      code = "";
    }
    expect(code, `5xx must be a structured degrade with a code, got: ${text}`).not.toBe("");
  }
}

// --------------------------------------------------------------------------- //
// Scenario 1 — Cluster lifecycle (stop / start) + idle auto-stop
// --------------------------------------------------------------------------- //
test.describe("scenario:aks-lifecycle", () => {
  test("auto-stop status returns a well-formed verdict", async ({ request }) => {
    const qs = new URLSearchParams({
      subscription_id: target.subscriptionId,
      resource_group: target.resourceGroup,
      cluster_name: target.clusterName,
    }).toString();
    const response = await getJson(request, `/api/aks/autostop/status?${qs}`);
    const text = await response.text();
    expectHealthyRead(response, text);
    expect(response.status()).toBe(200);
    const body = JSON.parse(text);
    expect(["stop", "warn", "keep", "disabled"]).toContain(body.verdict);
    expect(typeof body.reason).toBe("string");
    // active_job_count / seconds_until_stop are always present integers.
    expect(typeof body.active_job_count).toBe("number");
    expect(typeof body.seconds_until_stop).toBe("number");
    // cluster_power_state is the live AKS power or "" when unresolved — never null.
    expect(body).toHaveProperty("cluster_power_state");
  });

  test("auto-stop preference read returns the editable/enabled contract", async ({ request }) => {
    const qs = new URLSearchParams({
      subscription_id: target.subscriptionId,
      resource_group: target.resourceGroup,
      cluster_name: target.clusterName,
    }).toString();
    const response = await getJson(request, `/api/aks/autostop?${qs}`);
    const text = await response.text();
    expectHealthyRead(response, text);
    const body = JSON.parse(text);
    expect(body).toHaveProperty("enabled");
    expect(body).toHaveProperty("idle_minutes");
  });

  test("auto-stop extend (live mutation)", async ({ request }) => {
    test.skip(
      !allowAutostopMutate,
      "Set E2E_ALLOW_AKS_AUTOSTOP_MUTATE=1 (+ E2E_AZURE_* target) to extend a real auto-stop grant.",
    );
    const response = await postJson(request, "/api/aks/autostop/extend", {
      subscription_id: target.subscriptionId,
      resource_group: target.resourceGroup,
      cluster_name: target.clusterName,
      minutes: 30,
    });
    const text = await response.text();
    expect(response.status(), text).toBeLessThan(500);
    expect(response.status(), text).not.toBe(401);
  });

  test("cluster stop then start (live power mutation)", async ({ request }) => {
    test.skip(
      !allowAksPower,
      "Set E2E_ALLOW_AKS_POWER=1 (+ E2E_AZURE_* target) to stop/start a real AKS cluster (cost + minutes).",
    );
    const scope = {
      subscription_id: target.subscriptionId,
      resource_group: target.resourceGroup,
      cluster_name: target.clusterName,
    };
    const stop = await postJson(request, "/api/aks/stop", scope);
    expect(stop.status(), await stop.text()).toBeLessThan(500);
    const start = await postJson(request, "/api/aks/start", scope);
    expect(start.status(), await start.text()).toBeLessThan(500);
  });
});

// --------------------------------------------------------------------------- //
// Scenario 2 — Service Bus queue (settings, queue depth, enqueue → auto-start)
// --------------------------------------------------------------------------- //
test.describe("scenario:service-bus", () => {
  test("service bus settings/status returns the enable + entity contract", async ({ request }) => {
    const response = await getJson(request, "/api/settings/service-bus");
    const text = await response.text();
    expectHealthyRead(response, text);
    const body = JSON.parse(text);
    // The settings payload always reports the effective enable state and the
    // saved config (enabled nests under `config`), plus the kill-switch flags,
    // regardless of whether a namespace exists.
    expect(body).toHaveProperty("effective_enabled");
    expect(body.config).toHaveProperty("enabled");
    expect(body).toHaveProperty("counts");
  });

  test("service bus peek degrades gracefully when disabled/unreachable", async ({ request }) => {
    const response = await getJson(request, "/api/settings/service-bus/peek");
    const text = await response.text();
    // Peek may be 200 (messages/empty) or a structured 4xx when SB is not
    // configured — never a 5xx and never 401 under dev-bypass.
    expectHealthyRead(response, text);
  });

  test("service bus enqueue → drain → auto-start (live)", async ({ request }) => {
    test.skip(
      !allowSbSend,
      "Set E2E_ALLOW_SB_SEND=1 (+ a configured Service Bus namespace) to enqueue a real request-queue message.",
    );
    const response = await postJson(request, "/api/settings/service-bus/send", {
      program: "blastn",
      db: target.storageAccount ? `blast-db/16S_ribosomal_RNA/16S_ribosomal_RNA` : "16S_ribosomal_RNA",
      query_fasta: ">e2e_sb\nAGAGTTTGATCCTGGCTCAGATTGAACGCTGGCGGCAGGCCTAACACATGCAAGTCGAAC\n",
      external_correlation_id: `e2e-sb-${Date.now()}`,
    });
    const text = await response.text();
    expect(response.status(), text).toBeLessThan(500);
    expect(response.status(), text).not.toBe(401);
  });
});

// --------------------------------------------------------------------------- //
// Scenario 3 — Direct API (OpenAPI) BLAST: submit → status → results
// --------------------------------------------------------------------------- //
test.describe("scenario:api-blast", () => {
  test("external job list returns the public status vocabulary contract", async ({ request }) => {
    // Same external-discovery cost as /api/blast/jobs: this was measured at ~10s
    // locally (right at the default action timeout), so give it the same budget
    // to avoid a flaky timeout. The contract is reachability + sanitised degrade.
    test.setTimeout(60_000);
    const response = await getJson(request, "/api/v1/elastic-blast/jobs", 45_000);
    // When the OpenAPI plane is not configured (e.g. local dev without
    // ELB_OPENAPI_BASE_URL) this is a STRUCTURED 503 `openapi_not_configured`,
    // not a crash; in a wired environment it is a 200 job list.
    await expectReachable(response);
  });

  test("openapi submit → poll status → results (live)", async ({ request }) => {
    test.skip(
      !allowBlastSubmit,
      "Set E2E_ALLOW_BLAST_SUBMIT=1 (+ E2E_AZURE_* + a prepared DB/cluster) to run a real OpenAPI BLAST.",
    );
    const submit = await postJson(request, "/api/v1/elastic-blast/submit", {
      program: "blastn",
      db: "16S_ribosomal_RNA",
      query_fasta:
        ">e2e_16s\nAGAGTTTGATCCTGGCTCAGATTGAACGCTGGCGGCAGGCCTAACACATGCAAGTCGAAC\n",
      external_correlation_id: `e2e-openapi-${Date.now()}`,
      options: { outfmt: 5 },
    });
    const submitText = await submit.text();
    expect(submit.status(), submitText).toBeLessThan(300);
    const job = JSON.parse(submitText);
    expect(job.job_id || job.external_correlation_id).toBeTruthy();
    // The caller polls /api/v1/elastic-blast/jobs/{id} until status is terminal;
    // the long completion wait is intentionally left to the azure-lifecycle lane.
  });
});

// --------------------------------------------------------------------------- //
// Scenario 4 — Queue management, max parallelism, queue-wait
// --------------------------------------------------------------------------- //
test.describe("scenario:queue-parallel", () => {
  test("jobs list merges local + external rows without 5xx", async ({ request }) => {
    // Locally there is no external ElasticBLAST cluster, so /api/blast/jobs runs
    // a best-effort external discovery (subscription cluster list + per-cluster
    // OpenAPI endpoint resolve) that is measured at ~20-25s on a dev box — far
    // over the default 10s action timeout — before it degrades to the local-only
    // Table list. Give it a generous budget; the contract under test is the
    // SHAPE (healthy non-5xx / non-401 read), not latency. In a wired
    // environment the discovery is cached and fast.
    test.setTimeout(60_000);
    const response = await getJson(request, "/api/blast/jobs?limit=5", 45_000);
    const text = await response.text();
    expectHealthyRead(response, text);
  });

  test("parallel pre-flight fan-in stays internally consistent (cost 0)", async ({ request }) => {
    test.setTimeout(120_000); // real ARM/Storage admission probes are slow locally
    const payload = {
      subscription_id: target.subscriptionId,
      resource_group: target.resourceGroup,
      region: target.region,
      program: "blastn",
      db: "blast-db/16S_ribosomal_RNA/16S_ribosomal_RNA",
      query_data: ">e2e\nAGAGTTTGATCCTGGCTCAGATTGAACGCTGGCGGCAGGCCTAACACATGCAAGTCGAAC\n",
      storage_account: target.storageAccount,
      aks_cluster_name: target.clusterName,
      disable_sharding: true,
      sharding_mode: "off",
      enable_warmup: false,
    };
    // Pre-flight creates no jobs, so a fan-in of N is cost-free and proves the
    // admission/capacity evaluator answers consistently under concurrency. The
    // real ARM/Storage admission probes can take ~10-15s locally, so allow a
    // generous per-request timeout.
    const responses = await Promise.all(
      Array.from({ length: 3 }, () =>
        postJson(request, "/api/blast/pre-flight", payload, 60_000),
      ),
    );
    for (const response of responses) {
      const text = await response.text();
      expectHealthyRead(response, text);
      const body = JSON.parse(text);
      expect(body).toHaveProperty("admission");
      expect(["would_accept", "would_reject", "accepted", "rejected"]).toContain(
        body.admission?.decision ?? body.decision,
      );
    }
  });

  test("parallel real submit fan-in respects the max-parallel ceiling (live)", async ({
    request,
  }) => {
    test.skip(
      !allowBlastSubmit,
      "Set E2E_ALLOW_BLAST_SUBMIT=1 to fire concurrent real submits and assert queue-position accounting.",
    );
    const mk = (n: number) => ({
      subscription_id: target.subscriptionId,
      resource_group: target.resourceGroup,
      region: target.region,
      program: "blastn",
      db: "16S_ribosomal_RNA",
      query_fasta: `>e2e_par_${n}\nAGAGTTTGATCCTGGCTCAGATTGAACGCTGGCGGCAGGCCTAACACATGCAAGTCGAAC\n`,
      external_correlation_id: `e2e-par-${n}-${Date.now()}`,
      options: { outfmt: 5 },
    });
    const responses = await Promise.all(
      [0, 1].map((n) => postJson(request, "/api/v1/elastic-blast/submit", mk(n))),
    );
    const ids = new Set<string>();
    for (const response of responses) {
      const text = await response.text();
      expect(response.status(), text).toBeLessThan(300);
      const body = JSON.parse(text);
      ids.add(String(body.job_id ?? body.external_correlation_id));
    }
    expect(ids.size).toBe(responses.length); // distinct job ids, no collision
  });
});

// --------------------------------------------------------------------------- //
// Scenario 5 — Permissions (identity is wired; full matrix lives in pytest)
// --------------------------------------------------------------------------- //
test.describe("scenario:permissions", () => {
  test("the API resolves a caller identity (dev-bypass) for protected routes", async ({
    request,
  }) => {
    const response = await getJson(request, "/api/me");
    const text = await response.text();
    expectHealthyRead(response, text);
    expect(response.status()).toBe(200);
    const body = JSON.parse(text);
    // /api/me echoes the resolved identity — under dev-bypass this is the
    // synthetic anonymous OID; in login mode it is the MSAL subject.
    expect(body).toHaveProperty("object_id");
  });
});
