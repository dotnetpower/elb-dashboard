# Configure preview reflects the existing cluster shape and outfmt parity columns

## Motivation

The BLAST job **Execution Steps → Configure** view renders the
`elastic-blast.ini`. For externally-submitted jobs (sibling OpenAPI / Service
Bus → Table sync) no real `queries/<jobId>/elastic-blast.ini` blob exists, so
the dashboard falls back to an on-demand preview generated from the stored job
payload (`_config_preview_from_payload`). That preview had two accuracy gaps:

1. **`machine-type` showed the generic `DEFAULT_SKU` fallback** (e.g.
   `Standard_E32s_v5`) instead of the cluster the job actually reused. The
   payload options omit `machine_type`, so `generate_config` applied its
   default — misleading for an existing-cluster run and inconsistent with the
   real node SKU.
2. **The tabular outfmt lacked the result-UI parity columns.** The actual run
   already injects `staxids` / `sscinames` / `stitle` / `qcovs` at submit
   (`enrich_tabular_outfmt`), but the preview rendered whatever bare layout the
   payload stored, so the displayed `-outfmt` did not match what ran.

## User-facing change

* The Configure preview now shows the **existing cluster's blastpool SKU and
  node count** ("the current cluster as-is") instead of the default fallback,
  resolved live and best-effort from `get_aks_cluster_snapshot`.
* The preview's `-outfmt` now lists the **result parity columns** (Description,
  Scientific name, Query Cover), matching the columns the result page reads —
  idempotently, so already-enriched layouts are unchanged.

Both are display-only refinements of the on-demand preview; they do not change
how any job runs (existing completed jobs are unaffected).

## API / IaC diff summary

* `api/services/sharding_precision.py`: new `set_outfmt_spec(options, spec)` —
  replaces/append the `-outfmt` specifier in a blast options string (mirrors the
  `outfmt_spec_value` tokenisation; unquoted multi-token form).
* `api/services/blast/job_state.py`: `_config_preview_from_payload` gains
  optional `credential` / `subscription_id` kwargs and two best-effort helpers —
  `_enrich_preview_outfmt` (parks the enriched tabular outfmt in
  `additional_options`, drops the conflicting bare `outfmt` field) and
  `_apply_existing_cluster_shape` (overrides `machine_type` / `num_nodes` from
  the live cluster snapshot).
* `api/routes/blast/results.py`: passes `credential` + `subscription_id` to the
  preview so the cluster lookup can run.

No infra changes.

## Validation evidence

* `uv run ruff check` — clean on all touched files.
* `uv run pytest -q api/tests/test_sharding_precision.py api/tests/test_blast_tasks.py`
  → 192 passed (new: `set_outfmt_spec` replace/append/equals-form cases;
  preview outfmt enrichment for `additional_options` and bare `outfmt`; cluster
  SKU/count override with a credential; no-lookup-without-credential backward
  compat).
* `uv run pytest -q api/tests/test_route_contracts.py api/tests/test_web_blast_parity_fixtures.py api/tests/test_blast_config_sharding.py`
  → 72 passed; `test_smoke.py -k config_preview` → 2 passed (route still serves
  the preview with the new kwargs).
