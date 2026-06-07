import { describe, expect, it } from "vitest";

import {
  compareSemver,
  githubCompareUrl,
  githubRepoBaseUrl,
  isCommitUpdateAvailable,
  isUpgradeAvailable,
  type UpgradeStatus,
} from "./upgrade";

function makeStatus(overrides: Partial<UpgradeStatus> = {}): UpgradeStatus {
  return {
    running_version: "1.4.0",
    running_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    running_revision: "ca-elb-dashboard--0000001",
    current_images: {},
    latest_version: "",
    latest_sha: "",
    latest_checked_at: "2026-06-05T00:00:00Z",
    latest_commit_sha: "",
    git_remote: "https://github.com/dotnetpower/elb-dashboard.git",
    track_commits: true,
    state: "idle",
    target_version: "",
    target_sha: "",
    job_id: "",
    started_by_oid: "",
    started_at: "",
    phase_detail: "",
    phase_progress: 0,
    build_log_blob: "",
    rollback_target: {},
    rollback_available_until: "",
    updated_at: "",
    green_revision: "",
    blue_revision: "",
    confirm_deadline: "",
    traffic_serving: "",
    ...overrides,
  };
}

describe("githubRepoBaseUrl", () => {
  it("normalises an HTTPS .git remote", () => {
    expect(githubRepoBaseUrl("https://github.com/dotnetpower/elb-dashboard.git")).toBe(
      "https://github.com/dotnetpower/elb-dashboard",
    );
  });

  it("normalises an HTTPS remote without .git and trailing slash", () => {
    expect(githubRepoBaseUrl("https://github.com/dotnetpower/elb-dashboard/")).toBe(
      "https://github.com/dotnetpower/elb-dashboard",
    );
  });

  it("normalises an SCP-style SSH remote", () => {
    expect(githubRepoBaseUrl("git@github.com:dotnetpower/elb-dashboard.git")).toBe(
      "https://github.com/dotnetpower/elb-dashboard",
    );
  });

  it("strips embedded credentials", () => {
    expect(
      githubRepoBaseUrl("https://x-access-token:secret@github.com/dotnetpower/elb-dashboard.git"),
    ).toBe("https://github.com/dotnetpower/elb-dashboard");
  });

  it("returns null for non-GitHub remotes", () => {
    expect(githubRepoBaseUrl("https://gitlab.com/foo/bar.git")).toBeNull();
    expect(githubRepoBaseUrl("")).toBeNull();
    expect(githubRepoBaseUrl(null)).toBeNull();
  });
});

describe("githubCompareUrl", () => {
  it("builds a compare URL from running commit to the latest commit sha", () => {
    const status = makeStatus({
      latest_commit_sha: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    });
    expect(githubCompareUrl(status, "aaaaaaa")).toBe(
      "https://github.com/dotnetpower/elb-dashboard/compare/aaaaaaa...bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    );
  });

  it("falls back to latest_sha (release tag commit) when no commit sha", () => {
    const status = makeStatus({
      latest_version: "1.5.0",
      latest_sha: "cccccccccccccccccccccccccccccccccccccccc",
    });
    expect(githubCompareUrl(status, "aaaaaaa")).toBe(
      "https://github.com/dotnetpower/elb-dashboard/compare/aaaaaaa...cccccccccccccccccccccccccccccccccccccccc",
    );
  });

  it("falls back to running_sha when no running commit stamp is supplied", () => {
    const status = makeStatus({
      latest_commit_sha: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    });
    expect(githubCompareUrl(status, "")).toBe(
      "https://github.com/dotnetpower/elb-dashboard/compare/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa...bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    );
  });

  it("returns null when both endpoints are the same", () => {
    const status = makeStatus({ latest_commit_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" });
    expect(githubCompareUrl(status, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")).toBeNull();
  });

  it("rejects a placeholder running stamp and falls back to running_sha", () => {
    const status = makeStatus({
      running_sha: "ffffffffffffffffffffffffffffffffffffffff",
      latest_commit_sha: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    });
    expect(githubCompareUrl(status, "dev")).toBe(
      "https://github.com/dotnetpower/elb-dashboard/compare/ffffffffffffffffffffffffffffffffffffffff...bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    );
  });

  it("returns null when the running ref is a placeholder and running_sha is empty", () => {
    const status = makeStatus({
      running_sha: "",
      latest_commit_sha: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    });
    expect(githubCompareUrl(status, "unknown")).toBeNull();
  });

  it("returns null when the target ref is non-hex", () => {
    const status = makeStatus({ latest_version: "1.5.0", latest_sha: "not-a-sha" });
    expect(githubCompareUrl(status, "aaaaaaa")).toBeNull();
  });

  it("returns null when there is no target ref", () => {
    expect(githubCompareUrl(makeStatus(), "aaaaaaa")).toBeNull();
  });

  it("returns null for a non-GitHub remote", () => {
    const status = makeStatus({
      git_remote: "https://gitlab.com/foo/bar.git",
      latest_commit_sha: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    });
    expect(githubCompareUrl(status, "aaaaaaa")).toBeNull();
  });
});

describe("isCommitUpdateAvailable (range guard parity)", () => {
  it("is true when the latest commit differs from the running short sha", () => {
    const status = makeStatus({
      latest_commit_sha: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    });
    expect(isCommitUpdateAvailable(status, "aaaaaaa")).toBe(true);
  });

  it("is false when the latest commit shares the running prefix", () => {
    const status = makeStatus({
      latest_commit_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    });
    expect(isCommitUpdateAvailable(status, "aaaaaaa")).toBe(false);
  });
});

describe("compareSemver (commit-qualified versions)", () => {
  it("ignores a -commit.<sha> suffix when ordering releases", () => {
    // Same release, commit-qualified running build → equal (not 'running newer').
    expect(compareSemver("0.2.0", "0.2.0-commit.6517596")).toBe(0);
    expect(compareSemver("0.2.0-commit.6517596", "0.2.0")).toBe(0);
    // A real release bump still orders correctly despite a commit suffix.
    expect(compareSemver("0.3.0", "0.2.0-commit.6517596")).toBeGreaterThan(0);
    expect(compareSemver("0.2.0-commit.aaaaaaa", "0.3.0")).toBeLessThan(0);
  });

  it("keeps plain semver ordering intact", () => {
    expect(compareSemver("1.5.0", "1.4.0")).toBeGreaterThan(0);
    expect(compareSemver("1.4.0", "1.4.0")).toBe(0);
  });
});

describe("isUpgradeAvailable with commit-qualified running_version", () => {
  it("does not flag a release update when only the commit differs", () => {
    // GHA now bakes running_version=0.2.0-commit.<sha>; latest release is 0.2.0.
    const status = makeStatus({
      running_version: "0.2.0-commit.6517596",
      latest_version: "0.2.0",
    });
    expect(isUpgradeAvailable(status)).toBe(false);
  });

  it("still flags a genuine release update", () => {
    const status = makeStatus({
      running_version: "0.2.0-commit.6517596",
      latest_version: "0.3.0",
    });
    expect(isUpgradeAvailable(status)).toBe(true);
  });
});
