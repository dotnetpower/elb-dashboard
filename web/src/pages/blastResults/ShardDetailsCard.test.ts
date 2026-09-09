import { describe, expect, it } from "vitest";

import { shardProgressLabel } from "./ShardDetailsCard";

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