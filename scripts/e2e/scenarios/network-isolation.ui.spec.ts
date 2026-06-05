import { test, expect } from "../fixtures/uiTest";

// Network-isolation / degraded-state UX.
//
// Production Storage is `publicNetworkAccess: Disabled` (charter §9). The
// dashboard must clearly surface (a) when the local browser cannot read the
// data plane ("access blocked"), (b) when a Storage account is in the
// incident-grade Public-allowed state, and (c) when a monitor card degrades
// because ARM/data-plane returned a network/auth error. These are driven by
// response fields the fixture can override per-test:
//   - `/api/blast/databases` → `public_access_disabled`
//   - `/api/monitor/storage` → `public_network_access`
//   - `/api/monitor/acr`     → `degraded` + `degraded_reason`
// All mocked, so no real private-endpoint topology is needed.

test("Storage data plane blocked → BLAST DB section shows 'access blocked'", async ({
  uiPage,
  uiMocks,
}) => {
  uiMocks.setResponse("databases", {
    databases: [],
    public_access_disabled: true,
    degraded: true,
    degraded_reason: "network_blocked",
  });

  await uiPage.goto("/");

  const pill = uiPage.getByText("access blocked", { exact: true });
  await expect(pill).toBeVisible();
  // The pill's tooltip explains it is a private-network limitation, not a bug.
  await expect(
    uiPage.locator('[title*="Storage is Private only"]').first(),
  ).toBeVisible();
});

test("Storage Public-allowed is surfaced as the incident-grade 'Public allowed' state", async ({
  uiPage,
  uiMocks,
}) => {
  // Enabled + defaultAction Allow is the posture the dashboard must flag.
  uiMocks.setResponse("storage", {
    name: "stelbe2e",
    region: "koreacentral",
    sku: "Standard_LRS",
    kind: "StorageV2",
    public_network_access: "Enabled",
    is_hns_enabled: false,
    containers: [],
  });

  await uiPage.goto("/");

  await expect(uiPage.getByText("Public allowed", { exact: true })).toBeVisible();
  await expect(
    uiPage.locator('[title*="Public endpoint is enabled"]').first(),
  ).toBeVisible();
});

test("Private-only Storage is surfaced as the steady-state 'Private only'", async ({
  uiPage,
}) => {
  // No override → fixture default is publicNetworkAccess: Disabled.
  await uiPage.goto("/");
  await expect(uiPage.getByText("Private only", { exact: true })).toBeVisible();
});

test("ACR network_blocked degraded state shows the 'Network blocked' card label", async ({
  uiPage,
  uiMocks,
}) => {
  uiMocks.setResponse("acr", {
    name: "acre2e",
    login_server: "acre2e.azurecr.io",
    sku: "Basic",
    degraded: true,
    degraded_reason: "network_blocked",
    expected_image_tags: {},
    actual_tags: {},
    build_details: [],
  });

  await uiPage.goto("/");

  await expect(uiPage.getByText("Network blocked", { exact: true })).toBeVisible();
});
