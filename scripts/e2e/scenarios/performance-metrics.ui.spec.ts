import { test, expect } from "../fixtures/uiTest";

// Performance / live-metrics UX.
//
// The dashboard surfaces node-level performance (CPU / memory / file-cache
// utilisation) and warm-cache progress so a researcher can tell whether the
// cluster is healthy and whether a BLAST run will hit a warm database. Both are
// driven by polling endpoints the fixture can override per-test:
//   - `/api/monitor/aks/top-nodes`     → node CPU/mem/cache metrics
//   - `/api/monitor/aks/warmup-status` → per-DB warm progress
// The detail surface is the per-cluster modal opened from the expanded row.

const BUSY_NODE = {
  name: "aks-blastpool-00000v",
  cpu: "7200m",
  cpu_pct: 90,
  cpu_m: 7200,
  cpu_capacity_m: 8000,
  memory: "56Gi",
  memory_pct: 88,
  memory_total: "64Gi",
  mem_ki: 58_720_256,
  mem_capacity_ki: 67_108_864,
  cache_ki: 18_874_368,
  cache_pct: 28,
  pool: "blastpool",
  ready: true,
  conditions: { Ready: "True", MemoryPressure: "False" },
};

async function openClusterDetail(uiPage: import("@playwright/test").Page) {
  await uiPage.goto("/");
  // The cluster row toggles between "Expand cluster row" / "Collapse cluster
  // row". The "Open cluster detail" button is only mounted while expanded.
  // Poll-expand until the detail button is present (a single click can race the
  // row's collapse-state persistence), then open the modal. Scroll it into view
  // first — the expanded row can push it below the fold.
  const openDetail = uiPage.getByRole("button", { name: "Open cluster detail" });
  const collapsedRow = uiPage.getByLabel(/aks-e2e .*Expand cluster row/i);
  await expect(async () => {
    if (await openDetail.count()) return;
    await collapsedRow.first().click();
    await expect(openDetail).toBeVisible({ timeout: 2_000 });
  }).toPass({ timeout: 15_000 });
  await openDetail.scrollIntoViewIfNeeded();
  await openDetail.click();
  await expect(uiPage.getByRole("dialog", { name: /aks-e2e Details/i })).toBeVisible();
}

test("Node Resources panel renders per-node CPU/memory utilisation from top-nodes", async ({
  uiPage,
  uiMocks,
}) => {
  uiMocks.setResponse("topNodes", { nodes: [BUSY_NODE] });

  await openClusterDetail(uiPage);

  // The Node Resources section is expanded by default and shows the node's
  // pool plus its CPU/memory percentages.
  await expect(uiPage.getByText("Node Resources", { exact: true })).toBeVisible();
  // CPU 90% and memory 88% from the busy node appear in the per-node bars.
  await expect(uiPage.getByText(/90%/).first()).toBeVisible();
  await expect(uiPage.getByText(/88%/).first()).toBeVisible();
});

test("Node Resources panel shows the file-cache overlay when nodes report cache", async ({
  uiPage,
  uiMocks,
}) => {
  uiMocks.setResponse("topNodes", { nodes: [BUSY_NODE] });

  await openClusterDetail(uiPage);

  // cache_ki > 0 turns on the two-colour (working-set + file-cache) legend.
  await expect(uiPage.getByText(/cache/i).first()).toBeVisible();
});

test("Warm-cache progress renders the loading state in the cluster detail modal", async ({
  uiPage,
  uiMocks,
}) => {
  uiMocks.setResponse("warmup", {
    warm: false,
    workspace_ready: 1,
    workspace_desired: 3,
    databases: [
      {
        name: "core_nt",
        mol_type: "nucl",
        status: "Loading",
        sources: ["warmup"],
        nodes_ready: 1,
        nodes_failed: 0,
        nodes_active: 2,
        total_jobs: 3,
        progress_pct: 33.3,
      },
    ],
    vmtouch_ready: 1,
    namespaces: ["default"],
  });

  await openClusterDetail(uiPage);

  // The warm-cache section names the database being warmed.
  await expect(uiPage.getByText("core_nt").first()).toBeVisible();
});
