import { test as base, expect, type Page } from "@playwright/test";

import { assertNoErrorBoundary, installClientIssueCollector } from "../scenarios/helpers/assertions";
import { seedWorkspaceConfig, workspaceConfig } from "../scenarios/helpers/workspace";
import { installCoreUiMocks, type UiMockState } from "./mockApi";

export interface UiFixtures {
  uiPage: Page;
  uiMocks: UiMockState;
}

export const test = base.extend<UiFixtures>({
  uiMocks: async ({ page }, use) => {
    await seedWorkspaceConfig(page);
    await page.addInitScript((config) => {
      window.localStorage.setItem("elb-resource-config", JSON.stringify(config));
      window.localStorage.setItem("elb-getting-started-dismissed", "1");
      window.sessionStorage.setItem("elb-getting-started-dismissed", "true");
      window.localStorage.setItem(
        "elb-prefs",
        JSON.stringify({
          __v: 1,
          theme: "light",
          telemetryEnabled: false,
          appInsightsConnectionString: "",
          appInsightsWorkspaceResourceId: "",
          previewCustomDbEnabled: true,
          previewLabToolsEnabled: true,
          previewLiveWallEnabled: true,
          previewTerminalEnabled: true,
        }),
      );
      for (const key of Object.keys(window.localStorage)) {
        if (key.startsWith("elb-card-collapsed-")) window.localStorage.removeItem(key);
      }
      window.__ELB_RUNTIME_CONFIG__ = {
        ...(window.__ELB_RUNTIME_CONFIG__ ?? {}),
        VITE_AUTH_DEV_BYPASS: "true",
        VITE_FEATURE_CUSTOM_DB: "true",
        VITE_FEATURE_LAB_TOOLS: "true",
        VITE_FEATURE_TERMINAL: "true",
      };
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          readText: async () => "",
          writeText: async () => undefined,
        },
      });
    }, workspaceConfig);
    const mocks = await installCoreUiMocks(page);
    await use(mocks);
  },

  uiPage: async ({ page, uiMocks: _uiMocks }, use) => {
    const collector = installClientIssueCollector(page);
    await use(page);
    await assertNoErrorBoundary(page);
    await collector.assertClean();
  },
});

export { expect };