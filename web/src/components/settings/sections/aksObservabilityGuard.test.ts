import { describe, expect, it } from "vitest";

import { isContainerInsightsEnableDisabled } from "./aksObservabilityGuard";

const ready = {
  canRead: true,
  appInsightsName: "appi-elb-dashboard",
  providerEnableAvailable: true,
  taskRunning: false,
  resolvingWorkspace: false,
} as const;

describe("isContainerInsightsEnableDisabled", () => {
  it("allows enable only after the provider is confirmed registered", () => {
    expect(isContainerInsightsEnableDisabled(ready)).toBe(false);
  });

  it.each([null, false])(
    "fails closed when provider availability is %s",
    (providerEnableAvailable) => {
      expect(
        isContainerInsightsEnableDisabled({
          ...ready,
          providerEnableAvailable,
        }),
      ).toBe(true);
    },
  );

  it("keeps other operation guards intact", () => {
    expect(
      isContainerInsightsEnableDisabled({ ...ready, taskRunning: true }),
    ).toBe(true);
    expect(
      isContainerInsightsEnableDisabled({ ...ready, resolvingWorkspace: true }),
    ).toBe(true);
    expect(
      isContainerInsightsEnableDisabled({ ...ready, appInsightsName: "" }),
    ).toBe(true);
    expect(
      isContainerInsightsEnableDisabled({ ...ready, canRead: false }),
    ).toBe(true);
  });
});
