import { describe, expect, it } from "vitest";

import { shardProgressLabel, shouldPollShardDetails } from "./ShardDetailsCard";

function shardResponse({
  active,
  truncated,
}: {
  active: number;
  truncated: boolean;
}) {
  return {
    schema_version: 1,
    parent_job_id: "parent",
    truncated,
    summary: {
      total: 1000,
      completed: 1000 - active,
      failed: 0,
      cancelled: 0,
      active,
      other: 0,
      terminal: 1000 - active,
      progress_percent: 100 - active / 10,
    },
    shards: [],
  };
}

describe("shardProgressLabel", () => {
  it("describes an empty split safely", () => {
    expect(
      shardProgressLabel({
        total: 0,
        completed: 0,
        failed: 0,
        cancelled: 0,
        active: 0,
        other: 0,
        terminal: 0,
        progress_percent: 0,
      }),
    ).toBe("No shard work");
  });

  it("reports settled children without treating failures as incomplete", () => {
    expect(
      shardProgressLabel({
        total: 5,
        completed: 3,
        failed: 1,
        cancelled: 0,
        active: 1,
        other: 0,
        terminal: 4,
        progress_percent: 80,
      }),
    ).toBe("4 of 5 settled");
  });
});

describe("shouldPollShardDetails", () => {
  it("keeps polling active rows and truncated non-terminal parents", () => {
    expect(shouldPollShardDetails(shardResponse({ active: 1, truncated: false }), "running"))
      .toBe(true);
    expect(shouldPollShardDetails(shardResponse({ active: 0, truncated: true }), "running"))
      .toBe(true);
  });

  it("stops after all visible rows settle when the response is complete or parent terminal", () => {
    expect(shouldPollShardDetails(shardResponse({ active: 0, truncated: false }), "running"))
      .toBe(false);
    expect(shouldPollShardDetails(shardResponse({ active: 0, truncated: true }), "completed"))
      .toBe(false);
  });
});