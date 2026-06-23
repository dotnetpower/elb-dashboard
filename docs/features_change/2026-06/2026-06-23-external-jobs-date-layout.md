# Date-tiered results layout for external (SB / OpenAPI) BLAST jobs

## Motivation

The date-tiered storage layout (`STORAGE_DATE_LAYOUT_ENABLED`, issue #67) wrote
results under `results/YYYY/MM/DD/<job_id>/` only for **native** dashboard jobs
(`POST /api/blast/jobs` stamps `JobState.results_prefix`). **External jobs**
(Service Bus queue + OpenAPI `/v1/jobs`) stayed flat at `results/<job_id>/`
because the **sibling** OpenAPI service owns their result write path: in Mode B
(inline FASTA, the dashboard's submit shape) `main.py` hardcoded
`results_url = {blob_base}/results/{job_id}` and ignored any caller-supplied
`results` field. So enabling the flag had no effect on the customer's actual
(SB-driven) workflow.

## User-facing change

External SB / OpenAPI jobs now land under the **same** `YYYY/MM/DD/` date
directory as native jobs when the layout is enabled. The dashboard forwards a
`results_prefix` of the shape `YYYY/MM/DD/` on every external submit; the sibling
appends its own job id and writes `results/<YYYY/MM/DD>/<openapi_job_id>/...`.
Reads are unaffected — the sibling lists/streams from its stored `results` URL,
so write and read follow the same path automatically. With the flag off
(default) nothing changes (flat layout).

## API / IaC diff summary

**Dashboard (`elb-dashboard`)**

* `api/services/storage/job_prefix.py`: new `dated_results_subdir()` returning
  the `YYYY/MM/DD/` date directory (no job id — the sibling appends its own).
* `api/services/external_blast.py`: `submit_job` (the single choke point every
  external submit surface flows through — SB drain, the XML
  `/api/v1/elastic-blast/submit` direct path, and the canonical external submit)
  injects `results_prefix = dated_results_subdir()` when `date_layout_enabled()`
  and the caller has not already set one. Best-effort: never fails a submit.

**Sibling (`elastic-blast-azure`, separate repo — coordinated change)**

* `docker-openapi/app/schemas.py`: `JobSubmitRequest` gains an optional
  `results_prefix` field.
* `docker-openapi/app/main.py`: new `_validate_results_prefix()` (accepts only an
  exact `YYYY/MM/DD/` shape — rejects `..` / absolute / extra segments). Mode B
  `results_url` becomes `{blob_base}/results/{prefix}{job_id}`; an empty prefix
  keeps the flat layout. The `_discover_elb_job_id_from_submit_output` regex is
  made date-aware (its `job-<32hex>` fallback already covered it).

No managed-DB / Service Bus / SAS changes. Storage stays private.

## Permanence (deploy wiring)

`STORAGE_DATE_LAYOUT_ENABLED` is wired through both deploy paths as a default-OFF
gate with a per-deployment override (mirrors `SERVICEBUS_ENABLED`):

* `infra/control-plane-env.json`: added to `api` / `worker` / `beat` with the
  repo default `"false"` (charter §12a Rule 4). `scripts/dev/quick-deploy.sh`
  emits it as `--set-env-vars` on every api/worker/beat PATCH; a process/azd-env
  override of `true` wins over the JSON default.
* `infra/modules/containerAppControl.bicep`: new `storageDateLayoutEnabled`
  param + `effectiveStorageDateLayout` var + a `STORAGE_DATE_LAYOUT_ENABLED` env
  entry on all three python sidecars, so a full `azd provision` applies the same
  value.
* `infra/main.bicep` + `infra/main.parameters.json`: the param flows from the
  azd env var `STORAGE_DATE_LAYOUT_ENABLED`.

A deployment pins it on with `azd env set STORAGE_DATE_LAYOUT_ENABLED true`; the
repo default stays OFF so other environments are unaffected.

## Validation evidence

* Sibling: `python -m pytest tests/test_results_prefix.py tests/test_external_payload_hardening.py -q` → 22 passed (new validator: valid date / empty-flat / traversal-rejection cases).
* Dashboard: `uv run pytest -q api/tests/test_external_date_layout.py` → 3 passed (inject-when-enabled / no-inject-when-disabled / caller-prefix-wins) + `test_storage_job_prefix.py` `dated_results_subdir` case.
* Regression sweep: `uv run pytest -q api/tests/test_external_blast_cluster_resolver.py api/tests/test_servicebus_v1_multitoken.py api/tests/test_external_blast_api.py` → 176 passed.
* Live end-to-end (SB outfmt-7 jobs land on the date path + download) recorded after the customer deploy.
