import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { PREVIEW_PREF_KEYS } from "@/hooks/usePreferences";

const E2E_ROOT = path.resolve(__dirname, "..", "..", "..", "scripts", "e2e");
const UI_FIXTURE_PATH = path.resolve(E2E_ROOT, "fixtures", "uiTest.ts");
const MOCK_API_PATH = path.resolve(E2E_ROOT, "fixtures", "mockApi.ts");

describe("e2e uiTest fixture preview prefs", () => {
  const fixtureText = readFileSync(UI_FIXTURE_PATH, "utf8");

  for (const prefKey of Object.values(PREVIEW_PREF_KEYS)) {
    it(`enables ${prefKey} so the matching route is mounted in ui-mock`, () => {
      const enabled = new RegExp(`${prefKey}\\s*:\\s*true`).test(fixtureText);
      expect(enabled, `${prefKey} must be set to true in scripts/e2e/fixtures/uiTest.ts`).toBe(true);
    });
  }
});

describe("e2e mockApi completed job timestamps", () => {
  const mockText = readFileSync(MOCK_API_PATH, "utf8");

  // The Recent searches view groups jobs by local day. If the fixture job
  // uses the deterministic `now` constant, it eventually slips into the
  // "Yesterday" bucket and breaks scenarios that click the "Today" group.
  it("uses a rolling recentNow timestamp for the completed fixture job", () => {
    expect(mockText).toMatch(/const recentNow\s*=\s*new Date\(/);
    const completedBlock = mockText.match(/const completedJob\s*=\s*\{[\s\S]*?\n\s*\};/);
    expect(completedBlock, "completedJob declaration must be locatable").not.toBeNull();
    expect(completedBlock?.[0]).toMatch(/created_at:\s*recentNow/);
    expect(completedBlock?.[0]).toMatch(/updated_at:\s*recentNow/);
  });
});
