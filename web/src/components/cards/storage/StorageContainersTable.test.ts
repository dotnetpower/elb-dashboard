import { describe, expect, it } from "vitest";

import {
  formatContainerUsage,
  formatBytes,
  isPlatformContainer,
  splitStorageContainers,
} from "./StorageContainersTable";

describe("StorageContainersTable helpers", () => {
  it("groups platform containers away from workspace data", () => {
    const grouped = splitStorageContainers([
      { name: "dead-letter" },
      { name: "results" },
      { name: "job-artifacts" },
      { name: "blast-db" },
      { name: "queries" },
    ]);

    expect(grouped.workspaceContainers.map((container) => container.name)).toEqual([
      "blast-db",
      "queries",
      "results",
    ]);
    expect(grouped.platformContainers.map((container) => container.name)).toEqual([
      "dead-letter",
      "job-artifacts",
    ]);
  });

  it("recognizes control-plane state containers", () => {
    expect(isPlatformContainer("audit")).toBe(true);
    expect(isPlatformContainer("job-payloads")).toBe(true);
    expect(isPlatformContainer("queries")).toBe(false);
  });

  it("formats byte totals compactly", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1536)).toBe("1.50 KiB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.00 MiB");
  });

  it("marks capped usage as a lower bound", () => {
    expect(
      formatContainerUsage({
        name: "blast-db",
        size_bytes: 1024,
        blob_count: 50_000,
        usage_truncated: true,
      }),
    ).toBe(">= 1.00 KiB · 50,000 blobs");
  });

  it("shows cold cache usage as background calculation", () => {
    expect(
      formatContainerUsage({
        name: "queries",
        usage_pending: true,
      }),
    ).toBe("calculating usage");
  });

  it("keeps per-container usage failures quiet but visible", () => {
    expect(
      formatContainerUsage({
        name: "blast-db",
        usage_error: "HttpResponseError",
      }),
    ).toBe("usage unavailable");
  });
});
