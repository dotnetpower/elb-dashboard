import { describe, expect, it } from "vitest";

import { formatBuildVersion } from "./buildVersion";

describe("formatBuildVersion", () => {
  it("renders A.B.<buildNumber> from a plain semver", () => {
    expect(formatBuildVersion("0.2.0", "271")).toBe("0.2.271");
  });

  it("strips a commit-qualified APP_VERSION suffix (cloud build)", () => {
    // build-images.yml bakes `<semver>-commit.<shortSha>` into APP_VERSION.
    expect(formatBuildVersion("0.2.0-commit.2d563cd", "271")).toBe("0.2.271");
  });

  it("strips a build-metadata (+) suffix", () => {
    expect(formatBuildVersion("1.4.0+exp.sha.5114f85", "12")).toBe("1.4.12");
  });

  it("falls back to the cleaned release version when build number is non-numeric", () => {
    expect(formatBuildVersion("0.2.0-commit.2d563cd", "0")).toBe("0.2.0");
    expect(formatBuildVersion("0.2.0", "")).toBe("0.2.0");
    expect(formatBuildVersion("0.2.0", "abc")).toBe("0.2.0");
  });

  it("falls back to the cleaned release version when it is not A.B.C", () => {
    expect(formatBuildVersion("0.2", "271")).toBe("0.2");
    expect(formatBuildVersion("dev", "271")).toBe("dev");
  });
});
