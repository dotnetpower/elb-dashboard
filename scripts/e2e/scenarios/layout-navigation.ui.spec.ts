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

test("compact viewport collapses Tools (Lab Tools / Terminal / API) into a More dropdown", async ({
  uiPage,
}) => {
  // Below the 1320px compact-nav breakpoint (but above the 720px mobile drawer),
  // Lab Tools / Terminal / API collapse into a single "Tools" overflow dropdown.
  // The default 1600px project viewport keeps them direct, so this scenario
  // explicitly narrows the viewport to exercise the responsive nav path.
  await uiPage.setViewportSize({ width: 1100, height: 900 });
  await uiPage.goto("/");

  const nav = uiPage.getByRole("navigation", { name: "Main navigation" });

  // At this tier API is NOT a direct nav link — it lives behind the Tools trigger.
  await expect(nav.getByRole("link", { name: /^API$/i })).toHaveCount(0);

  const tools = nav.getByRole("button", { name: /Tools/i });
  await expect(tools).toBeVisible();
  await expect(tools).toHaveAttribute("aria-expanded", "false");

  // Open the overflow dropdown; the panel (role=menu) exposes the hidden routes.
  await tools.click();
  await expect(tools).toHaveAttribute("aria-expanded", "true");
  const menu = nav.getByRole("menu", { name: "Tools" });
  await expect(menu).toBeVisible();

  // ESC closes the overflow dropdown (NavMoreDropdown key handler).
  await uiPage.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(tools).toHaveAttribute("aria-expanded", "false");

  // Reopen and navigate via the now-revealed API link.
  await tools.click();
  const apiLink = menu.getByRole("link", { name: /^API$/i });
  await expect(apiLink).toBeVisible();
  await apiLink.click();
  await expect(uiPage.getByText("ElasticBLAST API Reference").first()).toBeVisible();
});