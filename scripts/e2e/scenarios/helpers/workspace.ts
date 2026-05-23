import type { Page } from "@playwright/test";

export const workspaceConfig = {
  subscriptionId: "00000000-0000-0000-0000-000000000000",
  workloadResourceGroup: "rg-elb-e2e",
  acrResourceGroup: "rg-elb-e2e",
  acrName: "acre2e",
  storageAccountName: "stelbe2e",
  terminalResourceGroup: "rg-elb-e2e",
  terminalVmName: "vm-elb-terminal",
  region: "koreacentral",
};

export async function seedWorkspaceConfig(page: Page) {
  await page.addInitScript((config) => {
    window.localStorage.setItem("elb-resource-config", JSON.stringify(config));
    window.localStorage.setItem("elb-getting-started-dismissed", "1");
    window.localStorage.setItem("elb-theme", "light");
    window.sessionStorage.clear();
  }, workspaceConfig);
}