import { describe, expect, it } from "vitest";

import {
  buildBlastCommandPreview,
  formatOutfmt,
  formatRunSeconds,
  isExternalJob,
  taxonomyFilterLabel,
} from "./configFormat";

describe("formatOutfmt", () => {
  it("prefers the multi-token specifier from additional_options", () => {
    expect(
      formatOutfmt({ outfmt: 7, additional_options: '-outfmt "7 std staxids sscinames"' }),
    ).toBe("7 std staxids sscinames");
    expect(formatOutfmt({ additional_options: "-outfmt 7 std staxids" })).toBe(
      "7 std staxids",
    );
  });
  it("falls back to the bare outfmt code", () => {
    expect(formatOutfmt({ outfmt: 5 })).toBe("5");
    expect(formatOutfmt({ outfmt: "6" })).toBe("6");
  });
  it("returns em-dash when nothing is set", () => {
    expect(formatOutfmt({})).toBe("—");
    expect(formatOutfmt(undefined)).toBe("—");
    expect(formatOutfmt(null)).toBe("—");
  });
});

describe("taxonomyFilterLabel", () => {
  it("labels include vs exclude", () => {
    expect(taxonomyFilterLabel({ taxid: 3431483 })).toBe("include taxid 3431483");
    expect(taxonomyFilterLabel({ taxid: 3431483, is_inclusive: true })).toBe(
      "include taxid 3431483",
    );
    expect(taxonomyFilterLabel({ taxid: 3431483, is_inclusive: false })).toBe(
      "exclude taxid 3431483",
    );
  });
  it("returns null when no taxid filter", () => {
    expect(taxonomyFilterLabel({})).toBeNull();
    expect(taxonomyFilterLabel({ taxid: "" })).toBeNull();
    expect(taxonomyFilterLabel(undefined)).toBeNull();
  });
  it("honours explicit negative_taxids / taxids flags", () => {
    expect(taxonomyFilterLabel({ negative_taxids: "3431483" })).toBe(
      "exclude taxid 3431483",
    );
    expect(taxonomyFilterLabel({ taxids: "9606" })).toBe("include taxid 9606");
    // explicit negative wins over a positive taxid field
    expect(taxonomyFilterLabel({ taxid: 9606, negative_taxids: "3431483" })).toBe(
      "exclude taxid 3431483",
    );
  });
});

describe("formatRunSeconds", () => {
  it("formats seconds and minutes", () => {
    expect(formatRunSeconds(42)).toBe("42s");
    expect(formatRunSeconds(127)).toBe("2m 7s");
    expect(formatRunSeconds(60)).toBe("1m 0s");
  });
  it("returns em-dash for invalid input", () => {
    expect(formatRunSeconds(null)).toBe("—");
    expect(formatRunSeconds("x")).toBe("—");
    expect(formatRunSeconds(-5)).toBe("—");
  });
});

describe("isExternalJob", () => {
  it("treats queue/api as external, dashboard as internal", () => {
    expect(isExternalJob("servicebus")).toBe(true);
    expect(isExternalJob("external_api")).toBe(true);
    expect(isExternalJob("dashboard")).toBe(false);
    expect(isExternalJob(undefined)).toBe(false);
  });
});

describe("buildBlastCommandPreview", () => {
  it("builds a command from the captured options", () => {
    const cmd = buildBlastCommandPreview("blastn", "core_nt", {
      additional_options: '-outfmt "7 std staxids"',
      evalue: 0.01,
      word_size: 28,
      max_target_seqs: 50,
      taxid: 3431483,
      is_inclusive: false,
    });
    expect(cmd).toContain("blastn");
    expect(cmd).toContain("-db core_nt");
    expect(cmd).toContain('-outfmt "7 std staxids"');
    expect(cmd).toContain("-evalue 0.01");
    expect(cmd).toContain("-word_size 28");
    expect(cmd).toContain("-max_target_seqs 50");
    expect(cmd).toContain("-negative_taxids 3431483");
  });
  it("returns empty when there is nothing to render", () => {
    expect(buildBlastCommandPreview("", "core_nt", {})).toBe("");
    expect(buildBlastCommandPreview("blastn", "", null)).toBe("");
  });
});
