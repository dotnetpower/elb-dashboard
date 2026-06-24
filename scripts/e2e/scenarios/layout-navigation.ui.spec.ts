import { test, expect } from "../fixtures/uiTest";
import { LayoutPage } from "../pageObjects/layout";

// The ui-mock project runs at 1600px (playwright.e2e.config.ts), wider than the
// 1320px compact-nav breakpoint, so every nav item — including API — is a direct
// top-level link. The Tools "More ▾" dropdown only collapses Lab Tools/Terminal/
// API at ≤1320px. If the project viewport is ever narrowed below 1320px, the
// API route here must open the Tools dropdown before clicking the link.
const routes = [
  { nav: /Dashboard/i, marker: "ElasticBLAST Dashboard" },
  { nav: /Live Wall/i, marker: "Live Wall" },
  { nav: /New Search/i, marker: "ElasticBLAST New Search" },
  { nav: /BLAST Jobs/i, marker: "BLAST Jobs" },
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