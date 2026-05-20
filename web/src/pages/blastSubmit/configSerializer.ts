/**
 * BLAST submit form serialiser — used by:
 *   1. "Export config" on a finished job → downloads a portable JSON file.
 *   2. "Duplicate / Re-run" on a finished job → stashes the snapshot in
 *      sessionStorage and routes the researcher to /blast/submit which
 *      hydrates the form on mount.
 *   3. The /blast/submit page → reads the sessionStorage handoff and (in
 *      the future) imports a user-uploaded JSON config.
 *
 * The on-disk JSON shape is intentionally a documented contract. Bumping
 * the schema version requires a migration in `partialFormFromConfig`.
 */
import type { FormState } from "@/pages/blastSubmitModel";

export const BLAST_CONFIG_SCHEMA = "elb-dashboard.blast-config";
export const BLAST_CONFIG_VERSION = 1;

/** sessionStorage key used to hand a snapshot from BlastResults → BlastSubmit. */
export const PENDING_DUPLICATE_KEY = "blast.pendingDuplicate";

/**
 * Subset of FormState that round-trips through the JSON config. Cluster
 * selection is intentionally excluded because cluster names are
 * environment-specific (re-running a 6-month-old config in a fresh
 * subscription would otherwise point at a deleted cluster). The hydrated
 * form lets the cluster picker resolve to whatever is currently available.
 *
 * Everything is optional so partial / older snapshots still hydrate the
 * fields they understand without throwing.
 */
export type ExportableFormFields = Partial<
  Omit<FormState, "selectedCluster">
>;

export interface BlastConfigSnapshot {
  /** Constant string so unrelated JSON files can be rejected on import. */
  schema: typeof BLAST_CONFIG_SCHEMA;
  /** Bumped when the shape of `form` changes incompatibly. */
  version: number;
  /** ISO8601 UTC timestamp. */
  exported_at: string;
  /** Origin metadata — purely informational, never used for hydration. */
  source?: {
    job_id?: string;
    job_title?: string;
  };
  form: ExportableFormFields;
}

/**
 * Pick the form fields that are safe to persist + round-trip. Cluster
 * selection is dropped (see ExportableFormFields). Reset transient
 * sub-range fields (query_from / query_to) only if both are blank — they
 * are still legitimate research inputs.
 */
export function pickExportableForm(form: FormState): ExportableFormFields {
  return {
    program: form.program,
    db: form.db,
    query_data: form.query_data,
    query_from: form.query_from,
    query_to: form.query_to,
    job_title: form.job_title,
    evalue: form.evalue,
    max_target_seqs: form.max_target_seqs,
    outfmt: form.outfmt,
    word_size: form.word_size,
    gap_open: form.gap_open,
    gap_extend: form.gap_extend,
    match_score: form.match_score,
    mismatch_score: form.mismatch_score,
    low_complexity_filter: form.low_complexity_filter,
    short_query_adjust: form.short_query_adjust,
    max_matches_in_query_range: form.max_matches_in_query_range,
    mask_lookup_table_only: form.mask_lookup_table_only,
    mask_lowercase: form.mask_lowercase,
    species_repeat_filter: form.species_repeat_filter,
    repeat_filter_taxid: form.repeat_filter_taxid,
    additional_options: form.additional_options,
    taxid: form.taxid,
    taxid_label: form.taxid_label,
    taxid_rank: form.taxid_rank,
    is_inclusive: form.is_inclusive,
    optimize: form.optimize,
    enable_warmup: form.enable_warmup,
    sharding_mode: form.sharding_mode,
    db_auto_partition: form.db_auto_partition,
    disable_sharding: form.disable_sharding,
  };
}

export interface SerializeArgs {
  form: FormState;
  source?: {
    jobId?: string;
    jobTitle?: string;
  };
}

export function serializeFormToConfig({
  form,
  source,
}: SerializeArgs): BlastConfigSnapshot {
  return {
    schema: BLAST_CONFIG_SCHEMA,
    version: BLAST_CONFIG_VERSION,
    exported_at: new Date().toISOString(),
    source:
      source && (source.jobId || source.jobTitle)
        ? {
            ...(source.jobId ? { job_id: source.jobId } : {}),
            ...(source.jobTitle ? { job_title: source.jobTitle } : {}),
          }
        : undefined,
    form: pickExportableForm(form),
  };
}

/**
 * Defensive parser for a config JSON. Returns the partial form on success,
 * null if the JSON is missing/invalid or the schema doesn't match. Used
 * both by the import flow and by sessionStorage hydration.
 */
export function partialFormFromConfig(raw: unknown): ExportableFormFields | null {
  if (!raw || typeof raw !== "object") return null;
  const snapshot = raw as Partial<BlastConfigSnapshot>;
  if (snapshot.schema !== BLAST_CONFIG_SCHEMA) return null;
  if (typeof snapshot.version !== "number") return null;
  if (snapshot.version > BLAST_CONFIG_VERSION) {
    // Newer-than-known: refuse rather than silently dropping fields the
    // researcher may have intentionally configured.
    return null;
  }
  if (!snapshot.form || typeof snapshot.form !== "object") return null;
  return normaliseFormFields(snapshot.form);
}

/**
 * Translate a backend job payload (the BlastSubmitRequest body that was
 * submitted) into a hydratable form subset. The payload field names are
 * mostly identical to FormState; the chief differences are:
 *
 *   - Numbers vs. strings: word_size, gap_open, gap_extend, match_score,
 *     mismatch_score live as numbers on the wire but as strings on the
 *     form (the textbox uses "" to mean "default").
 *   - aks_cluster_name is dropped (environment-specific).
 *   - additional_options is hydrated verbatim — including the synthetic
 *     "-dust yes", "-query_loc N-M", "-reward", "-penalty" tokens that
 *     buildSubmitRequest may have appended. That is intentional: a
 *     researcher duplicating a job wants the *effective* options to match,
 *     and the explicit flags are harmless if the upstream toggles also
 *     hydrate (they'd be re-appended on the next submit but the BLAST
 *     CLI tolerates duplicates).
 */
export function partialFormFromJobPayload(
  payload: unknown,
): ExportableFormFields | null {
  if (!payload || typeof payload !== "object") return null;
  const p = payload as Record<string, unknown>;
  const fields: ExportableFormFields = {};

  // program is an enum — drop unrecognised values rather than corrupting form.
  if (typeof p.program === "string" && VALID_PROGRAMS.has(p.program)) {
    fields.program = p.program as ExportableFormFields["program"];
  }
  assignString(fields, "db", p.db);
  assignString(fields, "query_data", p.query_data);
  assignString(fields, "job_title", p.job_title);
  assignString(fields, "additional_options", p.additional_options);

  assignNumber(fields, "evalue", p.evalue);
  assignInt(fields, "max_target_seqs", p.max_target_seqs);
  assignInt(fields, "outfmt", p.outfmt);
  // Range guards: a stale snapshot might carry a negative max_target_seqs
  // or an evalue of -1; either would explode the BLAST CLI on the next
  // submit. Drop out-of-range values rather than silently passing them on.
  if (
    fields.evalue !== undefined &&
    (fields.evalue <= 0 || fields.evalue > 10)
  ) {
    delete fields.evalue;
  }
  if (
    fields.max_target_seqs !== undefined &&
    (fields.max_target_seqs < 1 || fields.max_target_seqs > 10_000)
  ) {
    delete fields.max_target_seqs;
  }
  if (
    fields.outfmt !== undefined &&
    (fields.outfmt < 0 || fields.outfmt > 18)
  ) {
    delete fields.outfmt;
  }
  // Numeric → string form fields (blank means "use BLAST default").
  fields.word_size = numericToString(p.word_size);
  fields.gap_open = numericToString(p.gap_open);
  fields.gap_extend = numericToString(p.gap_extend);
  fields.match_score = numericToString(p.match_score);
  fields.mismatch_score = numericToString(p.mismatch_score);

  // Query sub-range — backend stores as embedded "-query_loc N-M" in
  // additional_options; the structured fields aren't on the wire. Leave
  // blank rather than fabricating values.
  fields.query_from = "";
  fields.query_to = "";

  // Taxonomy.
  if (typeof p.taxid === "number" && Number.isFinite(p.taxid)) {
    fields.taxid = String(p.taxid);
  } else if (typeof p.taxid === "string") {
    fields.taxid = p.taxid;
  } else {
    fields.taxid = "";
  }
  fields.taxid_label = "";
  fields.taxid_rank = "";
  fields.is_inclusive = p.is_inclusive !== false; // default true

  // Filters / sharding / warmup.
  fields.low_complexity_filter = p.low_complexity_filter !== false;
  fields.short_query_adjust = p.short_query_adjust !== false;
  fields.max_matches_in_query_range = numericToString(p.max_matches_in_query_range) || "0";
  fields.mask_lookup_table_only = p.mask_lookup_table_only !== false;
  fields.mask_lowercase = Boolean(p.mask_lowercase);
  fields.species_repeat_filter = Boolean(p.species_repeat_filter);
  fields.repeat_filter_taxid = numericToString(p.repeat_filter_taxid) || "9606";
  fields.enable_warmup = Boolean(p.enable_warmup);
  fields.disable_sharding = Boolean(p.disable_sharding);
  if (
    p.sharding_mode === "off" ||
    p.sharding_mode === "approximate" ||
    p.sharding_mode === "precise"
  ) {
    fields.sharding_mode = p.sharding_mode;
  }
  if (typeof p.db_auto_partition === "boolean") {
    fields.db_auto_partition = p.db_auto_partition;
  } else if (fields.sharding_mode) {
    fields.db_auto_partition = fields.sharding_mode !== "off";
  }
  if (typeof p.optimize === "string") {
    fields.optimize = p.optimize;
  }

  return fields;
}

/** Trigger a JSON download in the browser without any external library. */
export function downloadConfigJson(
  snapshot: BlastConfigSnapshot,
  filename: string,
): void {
  const body = JSON.stringify(snapshot, null, 2);
  const blob = new Blob([body], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
  } finally {
    URL.revokeObjectURL(url);
  }
}

/** Build a filesystem-safe download filename for a job's config. */
export function buildConfigFilename(meta: {
  jobId?: string;
  jobTitle?: string;
}): string {
  const base =
    (meta.jobTitle && safeFilenameFragment(meta.jobTitle)) ||
    (meta.jobId && safeFilenameFragment(meta.jobId)) ||
    "blast-config";
  return `${base}.config.json`;
}

// -------------------- helpers (not exported) --------------------

function assignString(
  target: ExportableFormFields,
  key: keyof ExportableFormFields,
  value: unknown,
): void {
  if (typeof value === "string") {
    // We rely on the FormState definition to accept the string. Cast is
    // local + narrow because the union of FormState string fields is wide.
    (target as Record<string, unknown>)[key] = value;
  }
}

function assignNumber(
  target: ExportableFormFields,
  key: keyof ExportableFormFields,
  value: unknown,
): void {
  if (typeof value === "number" && Number.isFinite(value)) {
    (target as Record<string, unknown>)[key] = value;
  }
}

function assignInt(
  target: ExportableFormFields,
  key: keyof ExportableFormFields,
  value: unknown,
): void {
  if (typeof value === "number" && Number.isInteger(value)) {
    (target as Record<string, unknown>)[key] = value;
  }
}

function numericToString(value: unknown): string {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "string") return value;
  return "";
}

function normaliseFormFields(raw: object): ExportableFormFields {
  // Whitelist filter — refuse unknown keys so a malicious / stale snapshot
  // can't poison FormState with arbitrary attributes.
  const ALLOWED: Array<keyof ExportableFormFields> = [
    "program",
    "db",
    "query_data",
    "query_from",
    "query_to",
    "job_title",
    "evalue",
    "max_target_seqs",
    "outfmt",
    "word_size",
    "gap_open",
    "gap_extend",
    "match_score",
    "mismatch_score",
    "low_complexity_filter",
    "short_query_adjust",
    "max_matches_in_query_range",
    "mask_lookup_table_only",
    "mask_lowercase",
    "species_repeat_filter",
    "repeat_filter_taxid",
    "additional_options",
    "taxid",
    "taxid_label",
    "taxid_rank",
    "is_inclusive",
    "optimize",
    "enable_warmup",
    "sharding_mode",
    "db_auto_partition",
    "disable_sharding",
  ];
  const out: ExportableFormFields = {};
  const src = raw as Record<string, unknown>;
  for (const key of ALLOWED) {
    if (!(key in src)) continue;
    const value = src[key];
    // Per-field type validation. Drop anything that doesn't match the
    // expected shape rather than blindly merging it into FormState.
    if (key === "program") {
      if (typeof value === "string" && VALID_PROGRAMS.has(value)) {
        (out as Record<string, unknown>)[key] = value;
      }
    } else if (key === "sharding_mode") {
      if (typeof value === "string" && VALID_SHARDING_MODES.has(value)) {
        (out as Record<string, unknown>)[key] = value;
      }
    } else if (
      key === "evalue" ||
      key === "max_target_seqs" ||
      key === "outfmt"
    ) {
      if (typeof value === "number" && Number.isFinite(value)) {
        // Range guards mirror partialFormFromJobPayload: a malicious /
        // truncated config must not poison BLAST CLI invocation.
        if (key === "evalue" && (value <= 0 || value > 10)) continue;
        if (key === "max_target_seqs" && (value < 1 || value > 10_000)) continue;
        if (key === "outfmt" && (value < 0 || value > 18)) continue;
        (out as Record<string, unknown>)[key] = value;
      }
    } else if (
      key === "low_complexity_filter" ||
      key === "short_query_adjust" ||
      key === "mask_lookup_table_only" ||
      key === "mask_lowercase" ||
      key === "species_repeat_filter" ||
      key === "is_inclusive" ||
      key === "enable_warmup" ||
      key === "db_auto_partition" ||
      key === "disable_sharding"
    ) {
      if (typeof value === "boolean") {
        (out as Record<string, unknown>)[key] = value;
      }
    } else if (typeof value === "string") {
      (out as Record<string, unknown>)[key] = value;
    }
  }
  return out;
}

const VALID_PROGRAMS = new Set<string>([
  "blastn",
  "blastp",
  "blastx",
  "tblastn",
  "tblastx",
]);

const VALID_SHARDING_MODES = new Set<string>(["off", "approximate", "precise"]);

function safeFilenameFragment(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
}
