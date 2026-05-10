# Disable cloud job submission — direct kubectl submit

**Date**: 2026-05-11
**Type**: fix

## Motivation

When elastic-blast runs with `cloud_job_submission=True` (the default),
it creates a `submit-jobs` K8s pod that downloads `batch_list.txt` from
blob storage and uses it to generate individual BLAST job YAML files.

However, in our warm-cluster flow (prepare → submit with `reuse=true`),
the `batch_list.txt` file is never created because:

1. `prepare()` calls `_initialize_cluster(None)` with no queries
2. The warm-cluster shortcut in `_initialize_cluster` skips
   `_upload_job_template()` when queries are `None`
3. When `submit()` runs later, it creates a new `elb_job_id`
   (UUID) and uploads the template, but the `submit-jobs` pod
   expects `batch_list.txt` at `ELB_RESULTS/metadata/` —
   which was never written

The result: `submit-jobs` pod fails with
`"Job file or batch list not found in Azure Blob Storage"`.

## Fix

Set `ELB_DISABLE_JOB_SUBMISSION_ON_THE_CLOUD=1` in the Run Command
scripts for both `prepare` and `submit` activities. This makes
elastic-blast use `_generate_and_submit_jobs()` which:

- Splits queries client-side (on the VM)
- Renders batch YAML files locally
- Submits them directly via `kubectl apply`
- Does NOT depend on `batch_list.txt`

Additionally, ACR name and resource group must be included in the
submit API payload to avoid the `elb-unknown-azure-acr-name` default.

## Files Changed

- `api/activities/blast.py`: Added `ELB_DISABLE_JOB_SUBMISSION_ON_THE_CLOUD=1`
  to both `activity_run_elastic_blast_prepare` and
  `activity_run_elastic_blast_submit` Run Command scripts

## Validation

- Submitted `job-ba8a4b739481` with the fix
- All 4 BLAST batch jobs completed successfully in ~40s
- `SUCCESS.txt` marker created in blob storage
- 4 result files (`batch_000~003-blastn-16S_ribosomal_RNA.out.gz`) present
- Orchestrator status: `completed`
- No `submit-jobs` pod created (direct kubectl submission instead)
