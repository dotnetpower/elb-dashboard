import { describe, expect, it } from "vitest";

import { shouldConfirmDownloadBeforeAks } from "@/components/cards/storage/BlastDbClusterConfirm";

describe("shouldConfirmDownloadBeforeAks", () => {
  it("requires confirmation when no cluster topology is known", () => {
    expect(shouldConfirmDownloadBeforeAks()).toBe(true);
    expect(shouldConfirmDownloadBeforeAks({ hasCluster: null, nodeCount: null })).toBe(
      true,
    );
  });

  it("requires confirmation when AKS is missing or node count is unknown", () => {
    expect(shouldConfirmDownloadBeforeAks({ hasCluster: false, nodeCount: null })).toBe(
      true,
    );
    expect(shouldConfirmDownloadBeforeAks({ hasCluster: true, nodeCount: null })).toBe(
      true,
    );
    expect(shouldConfirmDownloadBeforeAks({ hasCluster: true, nodeCount: 0 })).toBe(true);
  });

  it("does not require confirmation when workload node count is known", () => {
    expect(shouldConfirmDownloadBeforeAks({ hasCluster: true, nodeCount: 4 })).toBe(
      false,
    );
  });
});
