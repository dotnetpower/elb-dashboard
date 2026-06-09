/**
 * Tests for the discovery RG merge helpers — lock in the behaviour that fixes
 * the "reader sees the SetupWizard" bug: a non-empty but incomplete direct ARM
 * list must not mask the elb workspace that the MI proxy can see.
 */
import { describe, expect, it } from "vitest";

import { hasElbWorkspace, mergeRgsByName } from "./discoveryRgs";

const SUB = "00000000-0000-0000-0000-000000000001";

describe("hasElbWorkspace", () => {
  it("returns false for RGs without elb-* tags", () => {
    expect(
      hasElbWorkspace(SUB, [
        { name: "rg-unrelated", location: "koreacentral", tags: { team: "x" } },
        { name: "rg-empty", location: "koreacentral" },
      ]),
    ).toBe(false);
  });

  it("returns true when at least one RG carries elb-* tags", () => {
    expect(
      hasElbWorkspace(SUB, [
        { name: "rg-unrelated", location: "koreacentral", tags: { team: "x" } },
        {
          name: "rg-elb-dashboard",
          location: "koreacentral",
          tags: { "elb-storage": "elbstg01" },
        },
      ]),
    ).toBe(true);
  });
});

describe("mergeRgsByName", () => {
  it("unions both lists by name", () => {
    const merged = mergeRgsByName(
      [{ name: "rg-a", location: "k" }],
      [{ name: "rg-b", location: "k" }],
    );
    expect(merged.map((r) => r.name).sort()).toEqual(["rg-a", "rg-b"]);
  });

  it("lets the secondary (MI proxy) tags win for the same RG name", () => {
    const merged = mergeRgsByName(
      [{ name: "rg-elb", location: "k", tags: {} }],
      [{ name: "rg-elb", location: "k", tags: { "elb-storage": "s" } }],
    );
    expect(merged).toHaveLength(1);
    expect(merged[0].tags).toEqual({ "elb-storage": "s" });
  });

  it("surfaces the workspace after merging an incomplete direct list", () => {
    // Direct ARM only returned an unrelated RG (RG-scoped reader); the MI proxy
    // returns the elb workspace. After merge, discovery can find it.
    const direct = [{ name: "rg-unrelated", location: "k", tags: { team: "x" } }];
    const mi = [
      {
        name: "rg-elb-dashboard",
        location: "k",
        tags: { "elb-storage": "elbstg01" },
      },
    ];
    expect(hasElbWorkspace(SUB, direct)).toBe(false);
    expect(hasElbWorkspace(SUB, mergeRgsByName(direct, mi))).toBe(true);
  });
});
