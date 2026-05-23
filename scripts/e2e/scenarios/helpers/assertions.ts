import { expect, type Page } from "@playwright/test";

export interface ClientIssue {
  type: "console" | "pageerror" | "requestfailed" | "api5xx";
  message: string;
  url?: string;
}

export function installClientIssueCollector(page: Page) {
  const issues: ClientIssue[] = [];

  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const text = message.text();
    if (/Failed to load resource/i.test(text)) return;
    issues.push({ type: "console", message: text });
  });

  page.on("pageerror", (error) => {
    issues.push({ type: "pageerror", message: error.message, url: page.url() });
  });

  page.on("requestfailed", (request) => {
    const url = request.url();
    if (!url.includes("/api/")) return;
    const errorText = request.failure()?.errorText ?? "request failed";
    if (errorText === "net::ERR_ABORTED") return;
    issues.push({
      type: "requestfailed",
      message: errorText,
      url,
    });
  });

  page.on("response", (response) => {
    const url = response.url();
    if (!url.includes("/api/")) return;
    if (response.status() < 500) return;
    issues.push({
      type: "api5xx",
      message: `HTTP ${response.status()} ${response.statusText()}`,
      url,
    });
  });

  return {
    issues,
    async assertClean() {
      await assertNoErrorBoundary(page);
      expect(issues).toEqual([]);
    },
  };
}

export async function assertNoErrorBoundary(page: Page) {
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.getByText("Something went wrong")).toHaveCount(0);
}