import { describe, expect, it } from "vitest";

import {
  describeBrowserConnectionSource,
  describeServerTelemetry,
  shouldShowProvisionResource,
} from "./telemetryHelpers";

describe("telemetry display state", () => {
  it("describes a deployment connection without implying that server telemetry is idle", () => {
    expect(describeBrowserConnectionSource("deployment", false)).toMatchObject({
      label: "Deployment connection",
      tone: "success",
    });
  });

  it("warns when a browser override is present but incomplete", () => {
    expect(describeBrowserConnectionSource("user", false)).toMatchObject({
      label: "Invalid override",
      tone: "warning",
    });
    expect(describeBrowserConnectionSource("user", true)).toMatchObject({
      label: "Browser override",
      tone: "success",
    });
  });

  it("keeps server configuration independent from the browser telemetry toggle", () => {
    expect(describeServerTelemetry(true, true)).toMatchObject({
      label: "Configured",
      tone: "success",
    });
  });

  it("distinguishes an unresolved server check from an unconfigured deployment", () => {
    expect(describeServerTelemetry(false, false)).toMatchObject({
      label: "Checking",
      tone: "muted",
    });
    expect(describeServerTelemetry(false, true)).toMatchObject({
      label: "Not configured",
      tone: "warning",
    });
  });

  it("offers provisioning only after confirming that no connection is available", () => {
    expect(
      shouldShowProvisionResource({
        deploymentConfigured: true,
        deploymentStatusResolved: true,
        userConnectionStringValid: false,
      }),
    ).toBe(false);
    expect(
      shouldShowProvisionResource({
        deploymentConfigured: false,
        deploymentStatusResolved: false,
        userConnectionStringValid: false,
      }),
    ).toBe(false);
    expect(
      shouldShowProvisionResource({
        deploymentConfigured: false,
        deploymentStatusResolved: true,
        userConnectionStringValid: true,
      }),
    ).toBe(false);
    expect(
      shouldShowProvisionResource({
        deploymentConfigured: false,
        deploymentStatusResolved: true,
        userConnectionStringValid: false,
      }),
    ).toBe(true);
  });
});
