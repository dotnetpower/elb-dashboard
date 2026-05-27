import { describe, expect, it } from "vitest";

import {
  blastDbBlockedReason,
  blastDbReadinessLabel,
  blastDbReadinessTone,
  getBlastDbReadiness,
  isBlastDbReady,
} from "@/utils/blastDbReady";

describe("getBlastDbReadiness", () => {
  it("treats copy_status.phase=completed as ready", () => {
    const r = getBlastDbReadiness({
      copy_status: { phase: "completed", success: 800, total_files: 800 },
      file_count: 800,
    });
    expect(r).toEqual({ ready: true });
    expect(isBlastDbReady({ copy_status: { phase: "completed" } })).toBe(true);
    expect(blastDbReadinessLabel(r)).toBe("Storage DB ready");
    expect(blastDbReadinessTone(r)).toBe("ok");
    expect(blastDbBlockedReason(r)).toBeNull();
  });

  it("treats copy_status.phase=copying as not ready with progress", () => {
    const r = getBlastDbReadiness({
      copy_status: { phase: "copying", success: 30, total_files: 800 },
      file_count: 30,
    });
    expect(r.ready).toBe(false);
    expect(r).toMatchObject({
      reason: "copying",
      phase: "copying",
      progress: { success: 30, total: 800 },
    });
    expect(blastDbReadinessLabel(r)).toBe("Downloading · 30/800 files");
    expect(blastDbReadinessTone(r)).toBe("loading");
    expect(blastDbBlockedReason(r)).toContain("30/800");
  });

  it("treats copy_status.phase=partial as blocked", () => {
    const r = getBlastDbReadiness({
      copy_status: { phase: "partial", success: 750, total_files: 800, failed: 50 },
    });
    expect(r.ready).toBe(false);
    expect(r).toMatchObject({ reason: "partial" });
    expect(blastDbReadinessLabel(r)).toBe("Partial copy · 750/800 files");
    expect(blastDbReadinessTone(r)).toBe("blocked");
    expect(blastDbBlockedReason(r)).toMatch(/Retry/);
  });

  it("treats copy_status.phase=init_failed as blocked", () => {
    const r = getBlastDbReadiness({
      copy_status: { phase: "init_failed", success: 0, total_files: 0 },
    });
    expect(r).toMatchObject({ ready: false, reason: "init_failed" });
    expect(blastDbReadinessLabel(r)).toBe("Copy init failed");
    expect(blastDbReadinessTone(r)).toBe("blocked");
  });

  it("treats copy_status.phase=cancelled as not ready", () => {
    const r = getBlastDbReadiness({ copy_status: { phase: "cancelled" } });
    expect(r).toMatchObject({ ready: false, reason: "cancelled" });
    expect(blastDbReadinessLabel(r)).toBe("Download cancelled");
  });

  it("treats unknown phase strings as not ready (forward compat)", () => {
    const r = getBlastDbReadiness({ copy_status: { phase: "verifying" } });
    expect(r).toMatchObject({ ready: false, reason: "unknown_phase", phase: "verifying" });
    expect(blastDbReadinessLabel(r)).toBe("Phase: verifying");
  });

  it("treats update_in_progress as not ready when no copy_status present", () => {
    const r = getBlastDbReadiness({
      update_in_progress: true,
      updating_to_source_version: "BLAST_DB-2026-05-20",
      file_count: 800,
    });
    expect(r).toMatchObject({
      ready: false,
      reason: "updating",
      updatingTo: "BLAST_DB-2026-05-20",
    });
    expect(blastDbReadinessLabel(r)).toBe("Updating to BLAST_DB-2026-05-20");
    expect(blastDbReadinessTone(r)).toBe("loading");
  });

  it("falls back to legacy file_count>0 when no copy_status / not updating", () => {
    expect(isBlastDbReady({ file_count: 800 })).toBe(true);
    expect(getBlastDbReadiness({ file_count: 0 })).toMatchObject({
      ready: false,
      reason: "empty",
    });
  });

  it("treats undefined/null as not ready", () => {
    expect(isBlastDbReady(undefined)).toBe(false);
    expect(isBlastDbReady(null)).toBe(false);
    expect(getBlastDbReadiness(null)).toMatchObject({ ready: false, reason: "empty" });
  });

  it("copying without total_files omits progress", () => {
    const r = getBlastDbReadiness({
      copy_status: { phase: "copying", success: 30, total_files: 0 },
    });
    expect(r).toMatchObject({ ready: false, reason: "copying", phase: "copying" });
    if (!r.ready) expect(r.progress).toBeUndefined();
    expect(blastDbReadinessLabel(r)).toBe("Downloading…");
  });
});
