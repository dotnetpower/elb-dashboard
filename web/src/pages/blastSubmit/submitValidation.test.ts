import { describe, expect, it } from "vitest";

import { deriveSubmitValidation } from "./submitValidation";
import { INITIAL, PROGRAMS, type FormState } from "@/pages/blastSubmitModel";

function makeForm(overrides: Partial<FormState> = {}): FormState {
  return {
    ...INITIAL,
    query_data: ">query\nATGC",
    db: "blast-db/core_nt/core_nt",
    ...overrides,
  };
}

function validate(form: FormState) {
  return deriveSubmitValidation({
    form,
    programMeta: PROGRAMS[0],
    subId: "sub-1",
    workloadRg: "rg-elb",
    storageAccount: "elbstg01",
    selectedCluster: {
      name: "elb-cluster",
      power_state: "Running",
      provisioning_state: "Succeeded",
    } as never,
    dbQueryData: { databases: [{ name: "core_nt", file_count: 800 }] } as never,
    dbQueryIsSuccess: true,
    warmupBlocked: false,
    selectedDbPlan: null,
    submitPending: false,
  });
}

describe("blast submit taxonomy readiness", () => {
  it("treats an empty optional taxonomy filter as ready", () => {
    const result = validate(makeForm({ taxid: "", taxid_label: "" }));

    expect(result.readySteps.find((step) => step.label === "Taxonomy")?.ok).toBe(true);
    expect(result.canSubmit).toBe(true);
  });

  it("blocks submit readiness when taxonomy input is invalid", () => {
    const result = validate(makeForm({ taxid: "not-a-taxid" }));

    expect(result.readySteps.find((step) => step.label === "Taxonomy")?.ok).toBe(false);
    expect(result.canSubmit).toBe(false);
    expect(result.missing.map((item) => item.text)).toContain(
      "Taxonomy taxid must be a positive integer",
    );
  });

  it("blocks submit while runtime data is still loading", () => {
    const result = deriveSubmitValidation({
      form: makeForm(),
      programMeta: PROGRAMS[0],
      subId: "sub-1",
      workloadRg: "rg-elb",
      storageAccount: "elbstg01",
      selectedCluster: {
        name: "elb-cluster",
        power_state: "Running",
        provisioning_state: "Succeeded",
      } as never,
      dbQueryData: { databases: [{ name: "core_nt", file_count: 800 }] } as never,
      dbQueryIsSuccess: true,
      warmupBlocked: false,
      selectedDbPlan: null,
      dataLoading: true,
      submitPending: false,
    });

    expect(result.canSubmit).toBe(false);
    expect(result.missing.map((item) => item.text)).toContain(
      "Runtime data is still loading",
    );
  });

  it("blocks submit while the selected DB is still being copied", () => {
    const result = deriveSubmitValidation({
      form: makeForm(),
      programMeta: PROGRAMS[0],
      subId: "sub-1",
      workloadRg: "rg-elb",
      storageAccount: "elbstg01",
      selectedCluster: {
        name: "elb-cluster",
        power_state: "Running",
        provisioning_state: "Succeeded",
      } as never,
      dbQueryData: {
        databases: [
          {
            name: "core_nt",
            file_count: 30,
            copy_status: { phase: "copying", success: 30, total_files: 800 },
          },
        ],
      } as never,
      dbQueryIsSuccess: true,
      warmupBlocked: false,
      selectedDbPlan: null,
      submitPending: false,
    });

    expect(result.dbNotReady).toBe(true);
    expect(result.canSubmit).toBe(false);
    expect(
      result.readySteps.find((step) => step.label === "Database")?.ok,
    ).toBe(false);
    expect(
      result.missing.map((item) => item.text).some((text) => /Download in progress/.test(text)),
    ).toBe(true);
  });
});
