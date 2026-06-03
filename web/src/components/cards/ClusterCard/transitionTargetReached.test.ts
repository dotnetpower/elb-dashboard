import { describe, expect, it } from "vitest";

import { transitionTargetReached } from "./useClusterActions";

describe("transitionTargetReached", () => {
  it("keeps the starting chip while AKS reports Running but is still provisioning", () => {
    // AKS flips power_state to "Running" the instant a start LRO begins while
    // provisioning_state stays "Starting". The optimistic chip must persist so
    // the row does not flap to "Cluster is stopped" against a stale snapshot.
    expect(
      transitionTargetReached("starting", {
        power_state: "Running",
        provisioning_state: "Starting",
      }),
    ).toBe(false);
  });

  it("keeps the starting chip against a stale pre-start Stopped snapshot", () => {
    expect(
      transitionTargetReached("starting", {
        power_state: "Stopped",
        provisioning_state: "Succeeded",
      }),
    ).toBe(false);
  });

  it("clears the starting chip only once the cluster has settled into Running", () => {
    expect(
      transitionTargetReached("starting", {
        power_state: "Running",
        provisioning_state: "Succeeded",
      }),
    ).toBe(true);
  });

  it("keeps the stopping chip until provisioning settles", () => {
    expect(
      transitionTargetReached("stopping", {
        power_state: "Stopped",
        provisioning_state: "Stopping",
      }),
    ).toBe(false);
  });

  it("clears the stopping chip once the cluster has settled into Stopped", () => {
    expect(
      transitionTargetReached("stopping", {
        power_state: "Stopped",
        provisioning_state: "Succeeded",
      }),
    ).toBe(true);
  });

  it("never reports reached for a deleting transition (handled elsewhere)", () => {
    expect(
      transitionTargetReached("deleting", {
        power_state: "Stopped",
        provisioning_state: "Succeeded",
      }),
    ).toBe(false);
  });
});
