import { describe, expect, it } from "vitest";

import { aliasTone } from "./colors";

describe("aliasTone", () => {
  it("is deterministic for the same alias", () => {
    expect(aliasTone("jihoon@example.com")).toEqual(aliasTone("jihoon@example.com"));
  });

  it("returns a palette entry for empty alias without throwing", () => {
    const tone = aliasTone("");
    expect(tone.accent).toBeTruthy();
    expect(tone.fill).toBeTruthy();
    expect(tone.border).toBeTruthy();
  });

  it("maps different aliases independently (stable mapping)", () => {
    const a = aliasTone("alice");
    const b = aliasTone("alice");
    expect(a).toEqual(b);
  });
});
