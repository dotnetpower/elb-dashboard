import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { BlastDbRow } from "@/components/cards/storage/BlastDbRow";
import type { BlastDbCatalogItem } from "@/components/cards/storageDbCatalog";

const DB: BlastDbCatalogItem = {
  value: "core_nt",
  label: "Core nucleotide",
  desc: "Core nucleotide database",
  size: "large",
  estFiles: 10,
  estMinutes: "minutes",
  category: "Large",
  type: "nucl",
};

function renderRow(
  overrides: Partial<React.ComponentProps<typeof BlastDbRow>> = {},
): string {
  const noop = vi.fn();
  return renderToStaticMarkup(
    <BlastDbRow
      db={DB}
      meta={{
        source_version: "v2",
        copy_status: { phase: "completed" },
        db_order_oracle: {
          status: "ready",
          run_id: "ready-run",
          expected_parts: 10,
          ready_parts: 10,
        },
      }}
      isDownloaded
      isDownloading={false}
      isCopying={false}
      inProgressInfo={undefined}
      hasUpdate={false}
      latestVersion="v2"
      elapsed={0}
      downloadDisabled={false}
      oracleBuilding={false}
      oracleDisabled={false}
      autoWarmupChecked
      autoWarmupDisabled={false}
      autoOracleChecked
      autoOracleDisabled={false}
      autoOracleSaving={false}
      onDownload={noop}
      onUpdate={noop}
      onBuildOracle={noop}
      onConfirmLarge={noop}
      onToggleAutoWarmup={noop}
      onToggleAutoOracle={noop}
      onRetryAutoOracle={noop}
      {...overrides}
    />,
  );
}

describe("BlastDbRow Auto oracle", () => {
  it("shows the enabled control and current ready oracle", () => {
    const html = renderRow();

    expect(html).toContain("Auto oracle");
    expect(html).toContain("Order · ready");
    expect(html.match(/type="checkbox"[^>]*checked=""/g)).toHaveLength(2);
  });

  it("shows active rebuild progress without hiding current readiness", () => {
    const html = renderRow({
      meta: {
        source_version: "v2",
        copy_status: { phase: "completed" },
        db_order_oracle: {
          status: "ready",
          run_id: "ready-run",
          expected_parts: 10,
          ready_parts: 10,
          active: {
            status: "running",
            run_id: "build-run",
            expected_parts: 10,
            ready_parts: 4,
            automatic: true,
          },
        },
      },
    });

    expect(html).toContain("Order · 4/10");
    expect(html).toContain("Current oracle: 10/10 parts");
    expect(html).toContain("Ready");
  });

  it("shows an explicit Retry command only after retry exhaustion", () => {
    const html = renderRow({
      meta: {
        source_version: "v2",
        copy_status: { phase: "completed" },
        db_order_oracle: {
          status: "failed",
          automation: {
            status: "failed",
            failure_count: 3,
            retry_exhausted: true,
            last_error_code: "oracle_job_failed",
          },
        },
      },
    });

    expect(html).toContain("Retry Auto oracle for core_nt");
    expect(html).toContain("Retry");
  });

  it("keeps the control disabled when Auto warm is unavailable", () => {
    const html = renderRow({
      autoWarmupChecked: false,
      autoOracleChecked: false,
      autoOracleDisabled: true,
      autoOracleDisabledReason: "Enable Auto warm for this database first",
    });

    expect(html).toContain("Enable Auto warm for this database first");
    expect(html).toMatch(/<input type="checkbox"[^>]*disabled=""[^>]*\/>Auto oracle/);
  });
});
