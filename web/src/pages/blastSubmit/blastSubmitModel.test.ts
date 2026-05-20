import { describe, expect, it } from "vitest";

import {
  buildGeneratedJobTitle,
  formatJobTitleTimestamp,
} from "@/pages/blastSubmitModel";

describe("blast submit generated job titles", () => {
  it("formats timestamps as yyyymmdd-hhmm in local time", () => {
    const date = new Date(2026, 4, 19, 7, 8, 30);

    expect(formatJobTitleTimestamp(date)).toBe("20260519-0708");
  });

  it("prefixes generated titles with the timestamp", () => {
    const date = new Date(2026, 4, 19, 7, 8, 30);

    expect(buildGeneratedJobTitle("MPXV F3L - NC_003310.1", date)).toBe(
      "20260519-0708 MPXV F3L - NC_003310.1",
    );
  });
});