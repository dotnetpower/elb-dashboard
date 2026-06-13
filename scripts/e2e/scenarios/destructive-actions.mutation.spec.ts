import { test, expect } from "../fixtures/uiTest";

test("Dashboard destructive controls are isolated behind mocked mutations", async ({ uiPage, uiMocks }) => {
  await uiPage.goto("/");
  // The Stop/Delete actions are only mounted while the cluster row is expanded.
  // Poll-expand until the Stop button appears (a single click can race the
  // row's collapse-state persistence).
  const stopButton = uiPage.getByRole("button", { name: "Stop" });
  const collapsedRow = uiPage.getByLabel(/aks-e2e .*Expand cluster row/i);
  await expect(async () => {
    if (await stopButton.count()) return;
    await collapsedRow.first().click();
    await expect(stopButton).toBeVisible({ timeout: 2_000 });
  }).toPass({ timeout: 15_000 });

  // The dashboard polls cluster status on an interval, so the row re-renders
  // continuously and never satisfies Playwright's "stable" actionability check.
  // The button is visible + enabled, so click with force to skip the stability
  // wait, and retry until the mocked stop action is recorded.
  await expect(async () => {
    await stopButton.click({ force: true, timeout: 2_000 });
    expect(uiMocks.aksActions.map((row) => row.action)).toContain("stop");
  }).toPass({ timeout: 15_000 });

  // Same continuous-rerender row: force the Delete click, then drive the
  // type-to-confirm dialog.
  await uiPage.getByRole("button", { name: "Delete" }).click({ force: true });
  await expect(uiPage.getByRole("dialog", { name: /Delete cluster/i })).toBeVisible();
  // The delete dialog requires typing the cluster name to enable the confirm
  // button (type-to-confirm guard on irreversible AKS deletion).
  await uiPage.getByRole("textbox").fill("aks-e2e");
  await uiPage.getByRole("button", { name: /Permanently delete/i }).click();
  await expect.poll(() => uiMocks.aksActions.map((row) => row.action)).toContain("delete");
});

test("Storage database downloads and job deletion use mocked mutation endpoints", async ({ uiPage, uiMocks }) => {
  await uiPage.goto("/");
  await uiPage.getByTitle("Open database manager").click();
  await expect(uiPage.getByRole("dialog", { name: "BLAST Databases" })).toBeVisible();
  await uiPage.getByRole("button", { name: /^Get$/ }).first().click();
  await expect.poll(() => uiMocks.dbDownloads.length).toBeGreaterThan(0);
  await uiPage.keyboard.press("Escape");

  await uiPage.goto("/blast/jobs");
  await uiPage.getByTitle("Delete").click();
  await expect(uiPage.getByRole("dialog", { name: "Delete BLAST search" })).toBeVisible();
  await uiPage.getByRole("button", { name: /Permanently delete/i }).click();
  await expect.poll(() => uiMocks.jobDeletes.length).toBe(1);
});

test("Upgrade start, remote check, rollback, and escape commands are mocked", async ({ uiPage, uiMocks }) => {
  await uiPage.goto("/upgrade");
  await expect(uiPage.getByRole("heading", { name: "Self-upgrade" })).toBeVisible();

  await uiPage.getByRole("button", { name: "Check remote" }).click();
  await uiPage.locator("#upgrade-target").selectOption("0.3.0");
  await uiPage.getByLabel(/short downtime/i).check();
  await uiPage.getByRole("button", { name: /Start upgrade/i }).click();
  await expect.poll(() => uiMocks.upgradeStarts.length).toBe(1);

  await uiPage.route("**/api/upgrade/status", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        running_version: "0.3.0",
        running_sha: "2222222",
        running_revision: "rev-upgraded",
        current_images: { api: "api:0.3.0", frontend: "frontend:0.3.0", terminal: "terminal:0.3.0" },
        latest_version: "0.3.0",
        latest_sha: "2222222",
        latest_checked_at: "2026-05-24T10:00:00.000Z",
        git_remote: "origin",
        track_commits: true,
        latest_commit_sha: "",
        state: "succeeded",
        target_version: "0.3.0",
        target_sha: "2222222",
        target_kind: "release",
        job_id: "upgrade-e2e",
        started_by_oid: "e2e-user",
        started_at: "2026-05-24T10:00:00.000Z",
        phase_detail: "rollout complete",
        phase_progress: 100,
        build_log_blob: "upgrade-e2e.log",
        rollback_target: { api: "api:0.2.0", frontend: "frontend:0.2.0", terminal: "terminal:0.2.0" },
        rollback_available_until: "2026-05-25T10:00:00.000Z",
        updated_at: "2026-05-24T10:00:00.000Z",
      }),
    }),
  );
  await uiPage.reload();
  await uiPage.getByRole("button", { name: /Roll back/i }).click();
  await expect.poll(() => uiMocks.upgradeRollbacks).toBe(1);
  await uiPage.getByRole("button", { name: /Copy commands/i }).click();
});