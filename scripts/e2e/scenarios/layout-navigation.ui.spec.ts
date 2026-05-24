import { test, expect } from "../fixtures/uiTest";
import { LayoutPage } from "../pageObjects/layout";

const routes = [
  { nav: /Dashboard/i, marker: "ElasticBLAST Dashboard" },
  { nav: /Live Wall/i, marker: "Live Wall" },
  { nav: /New Search/i, marker: "ElasticBLAST New Search" },
  { nav: /Recent searches/i, marker: "Recent BLAST searches" },
  { nav: /^API$/i, marker: "ElasticBLAST API Reference" },
];

test("top navigation routes and chrome controls are event-safe", async ({ uiPage }) => {
  const layout = new LayoutPage(uiPage);
  await layout.goto("/");

  for (const route of routes) {
    await layout.navItem(route.nav).click();
    await expect(uiPage.getByText(route.marker).first()).toBeVisible();
  }

  await layout.openHelp();
  await uiPage.keyboard.press("Escape");
  await expect(uiPage.getByText("Help & Information")).toHaveCount(0);

  await layout.toggleTheme();
  await layout.openUserMenu();
  await uiPage.keyboard.press("Escape");
  await expect(uiPage.getByText("Microsoft Entra").or(uiPage.getByText(/Directory:/))).toHaveCount(0);
});