import { expect, type Page } from "@playwright/test";

export class LayoutPage {
  constructor(private readonly page: Page) {}

  async goto(path: string) {
    await this.page.goto(path);
    await expect(this.page.getByRole("navigation", { name: "Main navigation" })).toBeVisible();
  }

  navItem(name: string | RegExp) {
    return this.page.getByRole("navigation", { name: "Main navigation" }).getByRole("link", { name });
  }

  async openHelp() {
    await this.page.getByTitle("Keyboard shortcuts (?)").click();
    await expect(this.page.getByText("Help & Information")).toBeVisible();
  }

  async toggleTheme() {
    const currentTheme = await this.page.evaluate(() => document.documentElement.dataset.theme ?? "");
    await this.page.getByTitle("Settings").click();
    const dialog = this.page.getByRole("dialog", { name: "Settings" });
    await expect(dialog).toBeVisible();
    // Settings is section-navigated; select the Appearance section so the Theme
    // control is mounted (the panel may open on a different remembered section).
    await dialog.getByRole("button", { name: "Appearance" }).click();
    const themeGroup = dialog.getByRole("group", { name: "Theme" });
    await expect(themeGroup).toBeVisible();
    await themeGroup
      .getByRole("button", { name: currentTheme === "dark" ? /Light/i : /Dark/i })
      .click();
    await expect
      .poll(() => this.page.evaluate(() => document.documentElement.dataset.theme ?? ""))
      .not.toBe(currentTheme);
    await this.page.getByLabel("Close settings").click();
    await expect(this.page.getByRole("dialog", { name: "Settings" })).toHaveCount(0);
  }

  async openUserMenu() {
    await this.page.getByRole("button", { name: "User menu" }).click();
    await expect(this.page.getByText("Microsoft Entra").or(this.page.getByText(/Directory:/))).toBeVisible();
  }
}