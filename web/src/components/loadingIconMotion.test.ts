import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const SRC_ROOT = path.resolve(__dirname, "..");

function tsxFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const candidate = path.join(directory, entry.name);
    if (entry.isDirectory()) return tsxFiles(candidate);
    return entry.isFile() && entry.name.endsWith(".tsx") ? [candidate] : [];
  });
}

function relative(file: string): string {
  return path.relative(SRC_ROOT, file).replaceAll(path.sep, "/");
}

function iconTags(source: string, icon: string): string[] {
  return source.match(new RegExp(`<${icon}\\b[\\s\\S]*?\\/>`, "g")) ?? [];
}

describe("loading icon motion contract", () => {
  const files = tsxFiles(SRC_ROOT);

  it("animates every Loader2 through the shared spin contract", () => {
    const staticLoaders = files.flatMap((file) => {
      const source = readFileSync(file, "utf8");
      return iconTags(source, "Loader2")
        .filter((tag) => !/\bspin(?:-essential)?\b|animation\s*:/.test(tag))
        .map((tag) => `${relative(file)}: ${tag.replace(/\s+/g, " ")}`);
    });

    expect(staticLoaders).toEqual([]);
  });

  it.each([
    ["pages/apiReference/panelStates.tsx", "RefreshCw", "retrying"],
    ["pages/BlastJobs/JobsHeader.tsx", "RefreshCw", "jobsQuery.isFetching"],
    ["components/settings/sections/ServiceBusSection.tsx", "RefreshCw", "loading"],
    ["pages/ServiceBusPlayground.tsx", "RefreshCw", "observed.isFetching"],
    ["pages/UpgradePage.tsx", "RefreshCcw", "refreshing"],
    ["components/BuildLogViewer.tsx", "RefreshCcw", "loading"],
  ])("binds the active refresh icon in %s to %s", (relativePath, icon, stateExpression) => {
    const source = readFileSync(path.join(SRC_ROOT, relativePath), "utf8");
    const matchingTag = iconTags(source, icon).find(
      (tag) => tag.includes(stateExpression) && tag.includes("spin"),
    );

    expect(matchingTag).toBeDefined();
  });

  it("keeps the spin keyframes centralized in the shared stylesheet", () => {
    const localDefinitions = files
      .filter((file) => readFileSync(file, "utf8").includes("@keyframes spin"))
      .map(relative);

    expect(localDefinitions).toEqual([]);
  });

  it("keeps active loading feedback moving slowly under reduced motion", () => {
    const css = readFileSync(path.join(SRC_ROOT, "theme", "glass.css"), "utf8");

    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\) \{\s*\.spin\s*\{\s*animation: spin 2s linear infinite !important;\s*\}\s*\}/,
    );
  });
});
