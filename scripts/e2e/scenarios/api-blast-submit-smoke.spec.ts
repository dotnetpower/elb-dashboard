import { expect, test, type APIRequestContext } from "@playwright/test";

const apiUrl = process.env.E2E_API_URL ?? "http://127.0.0.1:8085";
const allowSubmit = process.env.E2E_ALLOW_BLAST_SUBMIT === "1";
const bearerToken = process.env.E2E_BEARER_TOKEN ?? "";

const smallBlastPayload = {
  subscription_id: process.env.E2E_AZURE_SUBSCRIPTION_ID ?? "00000000-0000-0000-0000-000000000000",
  resource_group: process.env.E2E_AZURE_RESOURCE_GROUP ?? "rg-elb-e2e",
  region: process.env.E2E_AZURE_REGION ?? "koreacentral",
  program: "blastn",
  db: process.env.E2E_BLAST_DB ?? "blast-db/16S_ribosomal_RNA/16S_ribosomal_RNA",
  query_data:
    ">e2e_16s_fragment\nAGAGTTTGATCCTGGCTCAGATTGAACGCTGGCGGCAGGCCTAACACATGCAAGTCGAAC\n",
  job_title: `e2e-api-smoke-${Date.now()}`,
  evalue: 0.05,
  max_target_seqs: 10,
  outfmt: 5,
  low_complexity_filter: true,
  enable_warmup: false,
  use_local_ssd: true,
  disable_sharding: true,
  sharding_mode: "off",
  storage_account: process.env.E2E_STORAGE_ACCOUNT ?? "stelbe2e",
  aks_cluster_name: process.env.E2E_AKS_CLUSTER ?? "aks-e2e",
  acr_resource_group: process.env.E2E_ACR_RESOURCE_GROUP,
  acr_name: process.env.E2E_ACR_NAME,
};

function authHeaders() {
  return bearerToken ? { Authorization: `Bearer ${bearerToken}` } : undefined;
}

async function postJson(request: APIRequestContext, path: string, data: unknown) {
  return request.post(`${apiUrl}${path}`, {
    data,
    headers: authHeaders(),
  });
}

test("pre-flight accepts the smallest BLAST smoke payload", async ({ request }) => {
  const response = await postJson(request, "/api/blast/pre-flight", smallBlastPayload);
  expect(response.status(), await response.text()).toBeLessThan(500);
  expect(response.status(), authHint(response.status())).not.toBe(401);

  const body = await response.json();
  expect(body).toHaveProperty("checks");
  expect(body).toHaveProperty("admission");
  expect(["would_accept", "would_reject", "accepted", "rejected"]).toContain(
    body.admission?.decision ?? body.decision,
  );
});

test("submits the smallest BLAST smoke payload when explicitly enabled", async ({ request }) => {
  test.skip(
    !allowSubmit,
    "Set E2E_ALLOW_BLAST_SUBMIT=1 to create a real BLAST job through /api/blast/jobs.",
  );

  const response = await postJson(request, "/api/blast/jobs", smallBlastPayload);
  expect(response.status(), await response.text()).toBeLessThan(500);
  expect(response.status(), authHint(response.status())).not.toBe(401);
  expect(response.status()).toBeLessThan(300);

  const body = await response.json();
  expect(body.job_id || body.id).toBeTruthy();
  expect(body.operation).toBeTruthy();
  expect(body.target).toBeTruthy();
  expect(body.admission?.decision).toBe("accepted");
});

function authHint(status: number): string {
  if (status !== 401) return `HTTP ${status}`;
  return "HTTP 401. In login mode, pass E2E_BEARER_TOKEN or run this smoke through dev-bypass.";
}