/**
 * Unit tests for the program ↔ database reconciliation helpers used by the
 * BLAST New Search program picker.
 *
 * Responsibility: Lock the molecule-type gating contract — which program tabs
 *   are selectable given the downloaded databases, and what happens to the
 *   selected DB when the researcher switches program (keep / overwrite / block).
 * Edit boundaries: Pure-function tests only; no React rendering, no network.
 * Key entry points: `deriveDbAvailabilityByType`, `decideProgramSwitch`,
 *   `resolveDbMoleculeType`.
 * Risky contracts: Unknown-type databases must stay permissive (never block a
 *   custom build); `undefined` databases (loading / manual path) must resolve
 *   to "switch" / "both available".
 * Validation: `npx vitest run src/pages/blastSubmit/programSelection.test.ts`.
 */
import { describe, expect, it } from "vitest";

import type { BlastDatabase } from "@/api/endpoints";
import {
  buildDatabasePath,
  decideProgramSwitch,
  deriveDbAvailabilityByType,
  resolveDbMoleculeType,
} from "@/pages/blastSubmit/helpers";
import { PROGRAMS } from "@/pages/blastSubmitModel";

const NUCL_PROGRAM = PROGRAMS.find((p) => p.value === "blastn")!;
const PROT_PROGRAM = PROGRAMS.find((p) => p.value === "blastp")!;

function makeDb(name: string, opts: Partial<BlastDatabase> = {}): BlastDatabase {
  return {
    name,
    container: "blast-db",
    copy_status: { phase: "completed" },
    ...opts,
  };
}

const coreNt = makeDb("core_nt"); // nucl, ready
const nr = makeDb("nr"); // prot, ready
const custom = makeDb("my_custom_build"); // unknown type, ready
const copyingNt = makeDb("nt", { copy_status: { phase: "copying", total_files: 4, success: 1 } });

describe("resolveDbMoleculeType", () => {
  it("classifies curated descriptions", () => {
    expect(resolveDbMoleculeType("core_nt")).toBe("nucl");
    expect(resolveDbMoleculeType("nr")).toBe("prot");
  });

  it("returns null for unknown / custom names", () => {
    expect(resolveDbMoleculeType("my_custom_build")).toBeNull();
  });
});

describe("buildDatabasePath", () => {
  it("joins container/prefix/name for folder-layout DBs", () => {
    expect(buildDatabasePath(makeDb("core_nt", { prefix: "core_nt" }))).toBe(
      "blast-db/core_nt/core_nt",
    );
  });

  it("uses the real directory for nested subset DBs (nt/nt_euk)", () => {
    // Regression: nt_euk files live under the nt/ folder, so the prefix is the
    // directory `nt`, not the base `nt_euk`. The path must resolve to the
    // actual blob stem the submit pre-flight checks.
    expect(buildDatabasePath(makeDb("nt_euk", { prefix: "nt" }))).toBe("blast-db/nt/nt_euk");
  });

  it("preserves multi-segment custom prefixes", () => {
    expect(buildDatabasePath(makeDb("labdb", { prefix: "custom_db/labdb" }))).toBe(
      "blast-db/custom_db/labdb/labdb",
    );
  });

  it("drops an empty prefix for top-level DB files (no double slash)", () => {
    expect(buildDatabasePath(makeDb("standalone", { prefix: "" }))).toBe("blast-db/standalone");
  });

  it("falls back to the DB name when prefix is undefined", () => {
    expect(buildDatabasePath(makeDb("legacy"))).toBe("blast-db/legacy/legacy");
  });
});

describe("deriveDbAvailabilityByType", () => {
  it("stays permissive when the list has not loaded", () => {
    expect(deriveDbAvailabilityByType(undefined)).toEqual({ nucl: true, prot: true });
  });

  it("blocks both when nothing is downloaded", () => {
    expect(deriveDbAvailabilityByType([])).toEqual({ nucl: false, prot: false });
  });

  it("unlocks only the molecule type that has a ready DB", () => {
    expect(deriveDbAvailabilityByType([coreNt])).toEqual({ nucl: true, prot: false });
    expect(deriveDbAvailabilityByType([nr])).toEqual({ nucl: false, prot: true });
    expect(deriveDbAvailabilityByType([coreNt, nr])).toEqual({ nucl: true, prot: true });
  });

  it("ignores databases that are not ready yet", () => {
    expect(deriveDbAvailabilityByType([copyingNt])).toEqual({ nucl: false, prot: false });
  });

  it("treats an unknown-type ready DB as compatible with both", () => {
    expect(deriveDbAvailabilityByType([custom])).toEqual({ nucl: true, prot: true });
  });
});

describe("decideProgramSwitch", () => {
  it("switches without DB reconciliation when the list has not loaded", () => {
    expect(decideProgramSwitch(PROT_PROGRAM, "blast-db/core_nt/core_nt", undefined)).toEqual({
      kind: "switch",
    });
  });

  it("keeps a compatible, ready database", () => {
    expect(decideProgramSwitch(NUCL_PROGRAM, buildDatabasePath(coreNt), [coreNt, nr])).toEqual({
      kind: "switch",
    });
  });

  it("keeps an unknown-type ready database (cannot prove incompatibility)", () => {
    expect(decideProgramSwitch(PROT_PROGRAM, buildDatabasePath(custom), [custom])).toEqual({
      kind: "switch",
    });
  });

  it("overwrites an incompatible database with a ready DB of the right type", () => {
    expect(decideProgramSwitch(PROT_PROGRAM, buildDatabasePath(coreNt), [coreNt, nr])).toEqual({
      kind: "switch-db",
      db: buildDatabasePath(nr),
    });
  });

  it("auto-selects a ready DB when none is selected yet", () => {
    expect(decideProgramSwitch(NUCL_PROGRAM, "", [coreNt, nr])).toEqual({
      kind: "switch-db",
      db: buildDatabasePath(coreNt),
    });
  });

  it("blocks when no ready DB of the required molecule type exists", () => {
    expect(decideProgramSwitch(PROT_PROGRAM, buildDatabasePath(coreNt), [coreNt])).toEqual({
      kind: "blocked",
      molecule: "prot",
    });
    expect(decideProgramSwitch(NUCL_PROGRAM, "", [copyingNt])).toEqual({
      kind: "blocked",
      molecule: "nucl",
    });
  });
});
