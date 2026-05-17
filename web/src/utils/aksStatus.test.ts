import { describe, expect, it } from "vitest";

import {
  getAksProvisioningLabel,
  isAksProvisioning,
  isAksProvisioningFailed,
  isAksWorkloadReady,
} from "./aksStatus";

describe("aksStatus", () => {
  it("does not treat a Creating cluster as workload-ready even when power is Running", () => {
    const cluster = { power_state: "Running", provisioning_state: "Creating" };

    expect(isAksWorkloadReady(cluster)).toBe(false);
    expect(isAksProvisioning(cluster)).toBe(true);
    expect(getAksProvisioningLabel(cluster)).toBe("Creating");
  });

  it("treats Running plus Succeeded as workload-ready", () => {
    expect(
      isAksWorkloadReady({ power_state: "Running", provisioning_state: "Succeeded" }),
    ).toBe(true);
  });

  it("surfaces Failed as a provisioning failure", () => {
    expect(isAksProvisioningFailed({ power_state: "Running", provisioning_state: "Failed" })).toBe(
      true,
    );
  });
});