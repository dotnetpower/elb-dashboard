/**
 * Tests for the BLAST submit form serialiser. Covers the three surfaces
 * that touch it: Export config (FormState → JSON), Import config
 * (JSON → FormState), and Duplicate / Re-run (job payload → FormState).
 */
import { describe, expect, it } from "vitest";

import { INITIAL, type FormState } from "@/pages/blastSubmitModel";

import {
  BLAST_CONFIG_SCHEMA,
  BLAST_CONFIG_VERSION,
  buildConfigFilename,
  partialFormFromConfig,
  partialFormFromJobPayload,
  pickExportableForm,
  serializeFormToConfig,
} from "./configSerializer";

function makeForm(overrides: Partial<FormState>): FormState {
  return { ...INITIAL, ...overrides };
}

describe("pickExportableForm", () => {
  it("drops selectedCluster (environment-specific)", () => {
    const fields = pickExportableForm(
      makeForm({ selectedCluster: "aks-elb-001" }),
    );
    expect("selectedCluster" in fields).toBe(false);
  });

  it("retains all researcher-facing parameters", () => {
    const form = makeForm({
      program: "blastp",
      db: "nr",
      query_data: ">q\nATCG",
      evalue: 1e-10,
      max_target_seqs: 500,
      outfmt: 6,
      word_size: "6",
      gap_open: "11",
      gap_extend: "1",
      taxid: "9606",
      taxid_label: "Homo sapiens",
      taxid_rank: "species",
      is_inclusive: false,
      additional_options: "-soft_masking true",
      low_complexity_filter: false,
      short_query_adjust: false,
      max_matches_in_query_range: "2",
      mask_lookup_table_only: false,
      mask_lowercase: true,
      species_repeat_filter: true,
      repeat_filter_taxid: "9606",
      enable_warmup: true,
      sharding_mode: "precise",
      db_auto_partition: true,
      disable_sharding: false,
    });
    const fields = pickExportableForm(form);
    expect(fields.program).toBe("blastp");
    expect(fields.db).toBe("nr");
    expect(fields.query_data).toBe(">q\nATCG");
    expect(fields.evalue).toBe(1e-10);
    expect(fields.max_target_seqs).toBe(500);
    expect(fields.outfmt).toBe(6);
    expect(fields.word_size).toBe("6");
    expect(fields.gap_open).toBe("11");
    expect(fields.gap_extend).toBe("1");
    expect(fields.taxid).toBe("9606");
    expect(fields.taxid_label).toBe("Homo sapiens");
    expect(fields.taxid_rank).toBe("species");
    expect(fields.is_inclusive).toBe(false);
    expect(fields.additional_options).toBe("-soft_masking true");
    expect(fields.low_complexity_filter).toBe(false);
    expect(fields.short_query_adjust).toBe(false);
    expect(fields.max_matches_in_query_range).toBe("2");
    expect(fields.mask_lookup_table_only).toBe(false);
    expect(fields.mask_lowercase).toBe(true);
    expect(fields.species_repeat_filter).toBe(true);
    expect(fields.repeat_filter_taxid).toBe("9606");
    expect(fields.enable_warmup).toBe(true);
    expect(fields.sharding_mode).toBe("precise");
    expect(fields.db_auto_partition).toBe(true);
  });
});

describe("serializeFormToConfig", () => {
  it("emits the schema tag, version, and ISO timestamp", () => {
    const snapshot = serializeFormToConfig({ form: makeForm({}) });
    expect(snapshot.schema).toBe(BLAST_CONFIG_SCHEMA);
    expect(snapshot.version).toBe(BLAST_CONFIG_VERSION);
    expect(new Date(snapshot.exported_at).toISOString()).toBe(
      snapshot.exported_at,
    );
  });

  it("includes source metadata when provided", () => {
    const snapshot = serializeFormToConfig({
      form: makeForm({}),
      source: { jobId: "job-123", jobTitle: "BRCA1 scan" },
    });
    expect(snapshot.source).toEqual({
      job_id: "job-123",
      job_title: "BRCA1 scan",
    });
  });

  it("omits source when both fields are missing", () => {
    const snapshot = serializeFormToConfig({
      form: makeForm({}),
      source: {},
    });
    expect(snapshot.source).toBeUndefined();
  });
});

describe("partialFormFromConfig", () => {
  it("returns null for non-objects", () => {
    expect(partialFormFromConfig(null)).toBeNull();
    expect(partialFormFromConfig("not an object")).toBeNull();
    expect(partialFormFromConfig(42)).toBeNull();
  });

  it("rejects snapshots with a foreign schema tag", () => {
    expect(
      partialFormFromConfig({
        schema: "evil.config",
        version: 1,
        form: { program: "blastn" },
      }),
    ).toBeNull();
  });

  it("rejects newer-than-known versions instead of dropping fields", () => {
    expect(
      partialFormFromConfig({
        schema: BLAST_CONFIG_SCHEMA,
        version: BLAST_CONFIG_VERSION + 1,
        form: { program: "blastn" },
      }),
    ).toBeNull();
  });

  it("round-trips a serialised form", () => {
    const original = makeForm({
      program: "blastx",
      db: "swissprot",
      evalue: 1e-5,
      max_target_seqs: 250,
      additional_options: "-soft_masking true",
      sharding_mode: "approximate",
    });
    const snapshot = serializeFormToConfig({ form: original });
    const round = partialFormFromConfig(JSON.parse(JSON.stringify(snapshot)));
    expect(round).not.toBeNull();
    expect(round!.program).toBe("blastx");
    expect(round!.db).toBe("swissprot");
    expect(round!.evalue).toBe(1e-5);
    expect(round!.additional_options).toBe("-soft_masking true");
    expect(round!.sharding_mode).toBe("approximate");
  });

  it("ignores unknown keys in the form blob", () => {
    const round = partialFormFromConfig({
      schema: BLAST_CONFIG_SCHEMA,
      version: BLAST_CONFIG_VERSION,
      form: { program: "blastn", __proto__pollution: "bad", arbitrary: 1 },
    });
    expect(round).not.toBeNull();
    expect(round!.program).toBe("blastn");
    expect("arbitrary" in (round as object)).toBe(false);
  });

  it("drops invalid program values (defence in depth)", () => {
    const round = partialFormFromConfig({
      schema: BLAST_CONFIG_SCHEMA,
      version: BLAST_CONFIG_VERSION,
      form: { program: "rm -rf /", db: "nt" },
    });
    expect(round).not.toBeNull();
    expect(round!.program).toBeUndefined();
    expect(round!.db).toBe("nt");
  });

  it("drops invalid sharding_mode values", () => {
    const round = partialFormFromConfig({
      schema: BLAST_CONFIG_SCHEMA,
      version: BLAST_CONFIG_VERSION,
      form: { sharding_mode: "wormhole" },
    });
    expect(round).not.toBeNull();
    expect(round!.sharding_mode).toBeUndefined();
  });

  it("drops booleans where strings are expected and vice versa", () => {
    const round = partialFormFromConfig({
      schema: BLAST_CONFIG_SCHEMA,
      version: BLAST_CONFIG_VERSION,
      form: {
        db: 42, // wrong type
        low_complexity_filter: "true", // wrong type
        evalue: "1e-5", // wrong type
      },
    });
    expect(round).not.toBeNull();
    expect(round!.db).toBeUndefined();
    expect(round!.low_complexity_filter).toBeUndefined();
    expect(round!.evalue).toBeUndefined();
  });

  it("drops out-of-range numeric fields from imported snapshots", () => {
    const round = partialFormFromConfig({
      schema: BLAST_CONFIG_SCHEMA,
      version: BLAST_CONFIG_VERSION,
      form: {
        evalue: -1,
        max_target_seqs: 0,
        outfmt: 99,
      },
    });
    expect(round).not.toBeNull();
    expect(round!.evalue).toBeUndefined();
    expect(round!.max_target_seqs).toBeUndefined();
    expect(round!.outfmt).toBeUndefined();
  });
});

describe("partialFormFromJobPayload", () => {
  it("returns null for non-objects", () => {
    expect(partialFormFromJobPayload(null)).toBeNull();
    expect(partialFormFromJobPayload("oops")).toBeNull();
  });

  it("translates a typical BlastSubmitRequest payload", () => {
    const payload = {
      program: "blastn",
      db: "core_nt",
      query_data: ">q\nACGT",
      job_title: "primer pair sanity",
      evalue: 0.001,
      max_target_seqs: 500,
      outfmt: 7,
      word_size: 28,
      gap_open: 5,
      gap_extend: 2,
      taxid: 9606,
      is_inclusive: false,
      additional_options: "-soft_masking true -dust yes",
      low_complexity_filter: false,
      enable_warmup: true,
      sharding_mode: "precise",
      db_auto_partition: true,
      disable_sharding: false,
      aks_cluster_name: "aks-elb-001",
    };
    const fields = partialFormFromJobPayload(payload);
    expect(fields).not.toBeNull();
    expect(fields!.program).toBe("blastn");
    expect(fields!.db).toBe("core_nt");
    expect(fields!.query_data).toBe(">q\nACGT");
    expect(fields!.evalue).toBe(0.001);
    expect(fields!.max_target_seqs).toBe(500);
    expect(fields!.outfmt).toBe(7);
    // Numeric → string conversion for form-field inputs.
    expect(fields!.word_size).toBe("28");
    expect(fields!.gap_open).toBe("5");
    expect(fields!.gap_extend).toBe("2");
    expect(fields!.taxid).toBe("9606");
    expect(fields!.is_inclusive).toBe(false);
    expect(fields!.low_complexity_filter).toBe(false);
    expect(fields!.enable_warmup).toBe(true);
    expect(fields!.sharding_mode).toBe("precise");
    expect(fields!.db_auto_partition).toBe(true);
    // Cluster name is environment-specific — must not be re-applied.
    expect("selectedCluster" in (fields as object)).toBe(false);
  });

  it("defaults is_inclusive to true when missing", () => {
    const fields = partialFormFromJobPayload({ program: "blastn" });
    expect(fields!.is_inclusive).toBe(true);
  });

  it("treats string numerics as-is for form text inputs", () => {
    const fields = partialFormFromJobPayload({
      program: "blastn",
      word_size: "11", // older payloads may have strings
    });
    expect(fields!.word_size).toBe("11");
  });

  it("blanks numeric fields when payload omits them", () => {
    const fields = partialFormFromJobPayload({ program: "blastp" });
    expect(fields!.word_size).toBe("");
    expect(fields!.gap_open).toBe("");
    expect(fields!.gap_extend).toBe("");
  });

  it("does not throw on weird sharding_mode values", () => {
    const fields = partialFormFromJobPayload({
      program: "blastn",
      sharding_mode: "rocket-science",
    });
    // Unknown mode is silently ignored — caller falls back to INITIAL.
    expect(fields!.sharding_mode).toBeUndefined();
  });

  it("rejects unrecognised program values in payload", () => {
    const fields = partialFormFromJobPayload({
      program: "<script>",
      db: "nt",
    });
    expect(fields).not.toBeNull();
    expect(fields!.program).toBeUndefined();
    expect(fields!.db).toBe("nt");
  });

  it("drops out-of-range numeric fields", () => {
    // evalue ≤ 0 or > 10 → drop.
    expect(
      partialFormFromJobPayload({ program: "blastn", evalue: -1 })!.evalue,
    ).toBeUndefined();
    expect(
      partialFormFromJobPayload({ program: "blastn", evalue: 0 })!.evalue,
    ).toBeUndefined();
    expect(
      partialFormFromJobPayload({ program: "blastn", evalue: 100 })!.evalue,
    ).toBeUndefined();
    // max_target_seqs out of [1, 10000].
    expect(
      partialFormFromJobPayload({ program: "blastn", max_target_seqs: 0 })!
        .max_target_seqs,
    ).toBeUndefined();
    expect(
      partialFormFromJobPayload({ program: "blastn", max_target_seqs: 1_000_000 })!
        .max_target_seqs,
    ).toBeUndefined();
    // outfmt out of [0, 18].
    expect(
      partialFormFromJobPayload({ program: "blastn", outfmt: -1 })!.outfmt,
    ).toBeUndefined();
    expect(
      partialFormFromJobPayload({ program: "blastn", outfmt: 99 })!.outfmt,
    ).toBeUndefined();
  });

  it("keeps in-range numeric fields untouched", () => {
    const fields = partialFormFromJobPayload({
      program: "blastn",
      evalue: 0.05,
      max_target_seqs: 100,
      outfmt: 5,
    });
    expect(fields!.evalue).toBe(0.05);
    expect(fields!.max_target_seqs).toBe(100);
    expect(fields!.outfmt).toBe(5);
  });
});

describe("buildConfigFilename", () => {
  it("prefers the job title when present", () => {
    expect(
      buildConfigFilename({ jobId: "abc-123", jobTitle: "BRCA1 scan v2" }),
    ).toBe("brca1-scan-v2.config.json");
  });

  it("falls back to the job id", () => {
    expect(buildConfigFilename({ jobId: "abc-123" })).toBe(
      "abc-123.config.json",
    );
  });

  it("falls back to a generic name", () => {
    expect(buildConfigFilename({})).toBe("blast-config.config.json");
  });

  it("strips dangerous filesystem characters", () => {
    const filename = buildConfigFilename({
      jobTitle: "../../etc/passwd ; rm -rf /",
    });
    // The download attribute is interpreted as a literal filename by the
    // browser, but it's worth keeping path separators and shell
    // metacharacters out of it so the saved file is unsurprising.
    expect(filename).not.toMatch(/[/\\;|&]/);
    expect(filename).toMatch(/\.config\.json$/);
    expect(filename).toContain("etc");
    expect(filename).toContain("passwd");
  });

  it("caps overly long titles", () => {
    const filename = buildConfigFilename({ jobTitle: "x".repeat(200) });
    expect(filename.length).toBeLessThanOrEqual("x".repeat(60).length + ".config.json".length);
  });
});
