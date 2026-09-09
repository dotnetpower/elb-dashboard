import { describe, expect, it } from "vitest";

import { INITIAL } from "@/pages/blastSubmitModel";

import {
  applyTemplateFields,
  pickTemplateFields,
  stripPerRunAdditionalOptions,
} from "./BlastTemplatesControl";

describe("pickTemplateFields", () => {
  it("keeps reusable scientific options and excludes query/per-run values", () => {
    const fields = pickTemplateFields({
      ...INITIAL,
      program: "blastn",
      db: "blast-db/core_nt/core_nt",
      query_data: ">private\nACGT",
      query_accession: "NC_000001.1",
      query_from: "1",
      query_to: "4",
      job_title: "private title",
      selectedCluster: "cluster-a",
      evalue: 1e-10,
      taxid: "9606",
      additional_options: "-dust yes -query_loc 1-4 -subject=private.fa -soft_masking true",
    });

    expect(fields.program).toBe("blastn");
    expect(fields.db).toBe("blast-db/core_nt/core_nt");
    expect(fields.evalue).toBe(1e-10);
    expect(fields.taxid).toBe("9606");
    expect(fields.additional_options).toBe("-dust yes -soft_masking true");
    expect(fields).not.toHaveProperty("query_data");
    expect(fields).not.toHaveProperty("query_accession");
    expect(fields).not.toHaveProperty("query_from");
    expect(fields).not.toHaveProperty("query_to");
    expect(fields).not.toHaveProperty("job_title");
    expect(fields).not.toHaveProperty("selectedCluster");
  });

  it("removes every per-run input flag form accepted by the template boundary", () => {
    expect(
      stripPerRunAdditionalOptions(
        '-query "private file.fa" -query_loc=2-8 -subject subject.fa ' +
          "-subject_loc '3-9' -evalue 1e-5",
      ),
    ).toBe("-evalue 1e-5");
  });

  it("applies options without replacing the current run input or identity", () => {
    const current = {
      ...INITIAL,
      query_data: ">current\nACGT",
      query_accession: "CURRENT",
      query_from: "2",
      query_to: "3",
      job_title: "current title",
      selectedCluster: "current-cluster",
    };
    const fields = {
      evalue: 1e-20,
      query_data: ">stale\nTTTT",
      query_accession: "STALE",
      query_from: "10",
      query_to: "20",
      job_title: "stale title",
    };

    expect(applyTemplateFields(current, fields)).toMatchObject({
      evalue: 1e-20,
      query_data: ">current\nACGT",
      query_accession: "CURRENT",
      query_from: "2",
      query_to: "3",
      job_title: "current title",
      selectedCluster: "current-cluster",
    });
  });
});