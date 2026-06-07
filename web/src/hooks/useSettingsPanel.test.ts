/**
 * Tests for the settings-panel section guard.
 *
 * `normalizeSettingsSection` protects deep-linking: a valid section id is
 * forwarded so the update indicator can land on "Updates", while a stray
 * argument (e.g. a click event passed by `onClick={open}`) is dropped so the
 * panel opens on its default section instead of an invalid one.
 */
import { describe, expect, it } from "vitest";

import { normalizeSettingsSection } from "./useSettingsPanel";

describe("normalizeSettingsSection", () => {
  it("passes through every known section id", () => {
    for (const id of [
      "appearance",
      "preview",
      "updates",
      "telemetry",
      "aks",
      "performance",
      "public-https",
      "vnet-peering",
      "sizing",
      "diagnostics",
      "resources",
    ]) {
      expect(normalizeSettingsSection(id)).toBe(id);
    }
  });

  it("drops a stray click-event argument so the panel opens on its default", () => {
    const fakeClickEvent = { type: "click", target: {}, currentTarget: {} };
    expect(normalizeSettingsSection(fakeClickEvent)).toBeUndefined();
  });

  it("drops undefined, unknown strings, and non-strings", () => {
    expect(normalizeSettingsSection(undefined)).toBeUndefined();
    expect(normalizeSettingsSection("not-a-section")).toBeUndefined();
    expect(normalizeSettingsSection(42)).toBeUndefined();
    expect(normalizeSettingsSection(null)).toBeUndefined();
  });
});
