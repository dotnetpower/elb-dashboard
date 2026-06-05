import { test, expect } from "../fixtures/uiTest";
import { READER_PERMISSIONS } from "../fixtures/mockApi";

// Restricted-user (subscription Reader + Storage Blob Data Reader) UX.
//
// A Reader can browse the dashboard but must NOT be able to mutate: the SPA
// disables Start/Stop, Delete, and Run BLAST and surfaces a permission-denied
// tooltip explaining the role they hold vs the role they need. This mirrors the
// real RBAC path the api sidecar computes in `/api/me/permissions`
// (`api/services/me_permissions.py`); here the capabilities are mocked so the
// gating UX can be asserted without a real restricted Azure principal.
//
// Why `degraded:false` matters: the SPA only disables a control when the
// capability is false AND the enumeration was NOT degraded. A degraded probe
// fails open (every button enabled) so a transient ARM hiccup never locks an
// operator out — see `READER_PERMISSIONS` and `usePermissions`.

test("Reader-only operator cannot mutate: Stop/Delete/Run BLAST are gated", async ({
  uiPage,
  uiMocks,
}) => {
  // Apply the restricted persona BEFORE navigating so `usePermissions` reads it
  // on its first fetch.
  uiMocks.setPermissions(READER_PERMISSIONS);

  await uiPage.goto("/");
  await uiPage.getByLabel(/aks-e2e .*Expand cluster row/i).click();

  // Stop is offered (cluster is Running) but disabled for a Reader.
  const stop = uiPage.getByRole("button", { name: "Stop" });
  await expect(stop).toBeDisabled();

  // Delete is always offered and disabled for a Reader.
  const del = uiPage.getByRole("button", { name: "Delete" });
  await expect(del).toBeDisabled();

  // The permission-denied tooltip explains the held vs required role.
  await expect(
    uiPage
      .locator('span[title*="do not have permission to start or stop this cluster"]')
      .first(),
  ).toBeVisible();
  await expect(
    uiPage.locator('span[title*="You hold: Reader"]').first(),
  ).toBeVisible();
});

test("Reader-only operator cannot submit a BLAST search", async ({ uiPage, uiMocks }) => {
  uiMocks.setPermissions(READER_PERMISSIONS);

  await uiPage.goto("/blast/submit");

  const run = uiPage.getByRole("button", { name: "Run BLAST" });
  await expect(run).toBeDisabled();
  // The Run button's tooltip is the permission-denied message (it wins over the
  // generic "fill in the required fields" hint).
  await expect(run).toHaveAttribute("title", /do not have permission to submit BLAST jobs/);
});

test("Reader-only operator cannot build ACR images", async ({ uiPage, uiMocks }) => {
  uiMocks.setPermissions(READER_PERMISSIONS);

  await uiPage.goto("/");

  // The ACR card's Build button is gated behind can_build_acr.
  const build = uiPage.getByRole("button", { name: /^Build$/ }).first();
  await expect(build).toBeDisabled();
  await expect(build).toHaveAttribute("title", /do not have permission to build ACR images/);
});

test("Full-access operator keeps the mutating controls enabled (control case)", async ({
  uiPage,
}) => {
  // No override → the fixture default is full access. This guards against a
  // false-positive where the buttons are disabled for an unrelated reason.
  await uiPage.goto("/");
  await uiPage.getByLabel(/aks-e2e .*Expand cluster row/i).click();

  await expect(uiPage.getByRole("button", { name: "Stop" })).toBeEnabled();
  await expect(uiPage.getByRole("button", { name: "Delete" })).toBeEnabled();
});
