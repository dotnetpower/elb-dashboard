import { describe, it, expect } from "vitest";

import { dbHasUpdate } from "./blastDbUpdates";

describe("dbHasUpdate", () => {
  const base = {
    isDownloaded: true,
    inUpdateMap: false,
    updatesEvaluated: true,
    latestVersion: "2026-06-09-01-05-01",
  };

  it("returns false for a DB that is not downloaded", () => {
    expect(
      dbHasUpdate({
        ...base,
        meta: { source_version: "2026-05-01-01-05-01" },
        isDownloaded: false,
        inUpdateMap: true,
      }),
    ).toBe(false);
  });

  it("returns false while an update is already in progress", () => {
    expect(
      dbHasUpdate({
        ...base,
        meta: {
          source_version: "2026-05-01-01-05-01",
          update_in_progress: true,
        },
        inUpdateMap: true,
      }),
    ).toBe(false);
  });

  it("returns true when the server lists the DB in its per-DB update map", () => {
    expect(
      dbHasUpdate({
        ...base,
        meta: { source_version: "2026-05-21-01-05-02" },
        inUpdateMap: true,
      }),
    ).toBe(true);
  });

  it("returns false when evaluated and absent from the map even though latest-dir rotated", () => {
    // The core regression: a DB the user JUST updated (or one whose content
    // is unchanged) must NOT show Update merely because its stored
    // source_version differs from the rotated latest-dir.
    expect(
      dbHasUpdate({
        ...base,
        meta: { source_version: "2026-05-21-01-05-02" },
        inUpdateMap: false,
        updatesEvaluated: true,
      }),
    ).toBe(false);
  });

  it("falls back to source_version diff only when the server did NOT evaluate", () => {
    expect(
      dbHasUpdate({
        ...base,
        meta: { source_version: "2026-05-21-01-05-02" },
        inUpdateMap: false,
        updatesEvaluated: false,
      }),
    ).toBe(true);
  });

  it("legacy fallback reports no update when source_version matches latest", () => {
    expect(
      dbHasUpdate({
        ...base,
        meta: { source_version: "2026-06-09-01-05-01" },
        inUpdateMap: false,
        updatesEvaluated: false,
      }),
    ).toBe(false);
  });

  it("legacy fallback is inert without a latestVersion or source_version", () => {
    expect(
      dbHasUpdate({
        ...base,
        meta: {},
        inUpdateMap: false,
        updatesEvaluated: false,
        latestVersion: null,
      }),
    ).toBe(false);
  });
});
