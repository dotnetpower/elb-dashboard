import { test, expect } from "../fixtures/uiTest";

test("mobile database manager keeps Auto oracle controls usable", async ({ uiPage }) => {
  await uiPage.setViewportSize({ width: 390, height: 844 });
  await uiPage.goto("/");
  await uiPage.getByTitle("Open database manager").click();

  const dialog = uiPage.getByRole("dialog", { name: "BLAST Databases" });
  await expect(dialog).toBeVisible();
  const dialogBox = await dialog.boundingBox();
  expect(dialogBox).not.toBeNull();
  expect(dialogBox!.x).toBeGreaterThanOrEqual(0);
  expect(dialogBox!.x + dialogBox!.width).toBeLessThanOrEqual(390);

  const coreNtRow = dialog.locator(".db-row").filter({ hasText: "core_nt" });
  const autoOracle = coreNtRow.getByRole("checkbox", { name: "Auto oracle" });
  await autoOracle.scrollIntoViewIfNeeded();
  await expect(autoOracle).toBeVisible();
  await expect(autoOracle).toBeChecked();

  const pageWidth = await uiPage.evaluate(() => {
    const client = document.documentElement.clientWidth;
    const offenders = [...document.querySelectorAll<HTMLElement>("body *")]
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return {
          tag: element.tagName,
          className: element.className,
          right: Math.round(rect.right),
          scrollWidth: element.scrollWidth,
          clientWidth: element.clientWidth,
        };
      })
      .filter(
        (element) =>
          element.right > client || element.scrollWidth > element.clientWidth + 1,
      );
    return {
      client,
      scroll: document.documentElement.scrollWidth,
      offenders: offenders.slice(0, 20),
    };
  });
  expect(
    pageWidth.scroll,
    `Horizontal overflow: ${JSON.stringify(pageWidth.offenders)}`,
  ).toBeLessThanOrEqual(pageWidth.client);

  await dialog.getByTitle("Close").click();
  await expect(dialog).toHaveCount(0);
});

test("small mobile header keeps navigation reachable", async ({ uiPage }) => {
  await uiPage.setViewportSize({ width: 320, height: 568 });
  await uiPage.goto("/");

  const pageWidth = await uiPage.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(pageWidth.scroll).toBeLessThanOrEqual(pageWidth.client);

  const toggle = uiPage.getByRole("button", { name: "Toggle navigation" });
  await toggle.click();
  const nav = uiPage.getByRole("navigation", { name: "Main navigation" });
  await expect(nav).toHaveClass(/layout__nav--open/);
  await nav.getByRole("link", { name: "New Search" }).click();

  await expect(uiPage).toHaveURL(/\/blast\/submit$/);
  await expect(nav).not.toHaveClass(/layout__nav--open/);
  await expect(uiPage.getByText("ElasticBLAST New Search").first()).toBeVisible();
});