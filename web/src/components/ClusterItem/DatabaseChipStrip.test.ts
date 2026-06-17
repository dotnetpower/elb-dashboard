import { describe, expect, it } from "vitest";

import { dbChipVisibleStatusMessage } from "./DatabaseChipStrip";
import type { DbChip } from "./types";

describe("dbChipVisibleStatusMessage", () => {
  it("shows active warmup progress without relying on hover text", () => {
    const db: DbChip = {
      name: "core_nt",
      sharded: true,
      shardLayouts: 8,
      shardingInProgress: false,
      shardingError: null,
      sourceVersion: "v:2026-05-21 01:05:02",
      warmSourceVersion: null,
      warmSourceVersions: [],
      warmStale: false,
      warm: {
        name: "core_nt",
        mol_type: "nucl",
        status: "Loading",
        nodes_ready: 0,
        nodes_active: 3,
        nodes_failed: 0,
        total_jobs: 3,
        active_phase: "copying_files",
        active_message: "copying 0/3",
        elapsed_seconds: 300,
      },
    };

    expect(dbChipVisibleStatusMessage(db, null)).toContain(
      "core_nt: copying DB cache (0/3 nodes ready, running 5m) - copying 0/3.",
    );
  });

  it("shows stale warm cache state inline", () => {
    const db: DbChip = {
      name: "core_nt",
      sharded: true,
      shardLayouts: 8,
      shardingInProgress: false,
      shardingError: null,
      sourceVersion: "v:2026-05-24 02:12:01",
      warmSourceVersion: "v:2026-05-21 01:05:02",
      warmSourceVersions: ["v:2026-05-21 01:05:02"],
      warmStale: true,
    };

    expect(dbChipVisibleStatusMessage(db, null)).toBe(
      "core_nt: node-local warm cache was cleared by a cluster stop or scale — re-warm to restore the fast sharded path.",
    );
  });
});