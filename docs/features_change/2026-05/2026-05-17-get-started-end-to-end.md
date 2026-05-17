# 2026-05-17 - End-to-end Get Started runbook

## Motivation

The previous get-started guide covered local setup and first Azure deployment, but stopped before a researcher could prove that a fresh clone actually reaches a complete ElasticBLAST result. The onboarding path needed one concrete, smallest-cost smoke test from prerequisites through AKS, database preparation, BLAST submit, and result download.

## User-facing change

- Rewrote `docs/get-started.md` as a phased runbook: tool install, clone, local checks, App Registration, `azd up`, redirect URI, deployed app sign-in, smallest BLAST smoke test, network lockdown, cleanup, and troubleshooting.
- Added a smallest end-to-end BLAST scenario using `16S_ribosomal_RNA`, `Standard_D2s_v3` system pool, `Standard_D8s_v3` workload pool, one workload node, `blastn`, XML output format 5, warmup off, and sharding off.
- Documented the required AKS Blob CSI / `azureblob-nfs-premium` check, the six-sidecar Container App check, terminal CLI verification, and the job-submit image rebuild caveat for Azure Blob NFS.
- Added an optional clean Azure VM validation appendix that creates an Ubuntu 24.04 `Standard_D4s_v5` VM with SSH restricted to the caller IP, then runs the Linux prerequisite, clone, backend test, and web build steps from the same document.
- Preserved the local API port correction to `127.0.0.1:8085`.

## API / IaC diff summary

- `api/tasks/azure.py`: AKS provisioning now builds a cluster model with the Blob CSI driver enabled so `azureblob-nfs-premium` is available for ElasticBLAST PV mode.
- `api/services/image_tags.py` and `api/tasks/acr.py`: the ACR build task now shell-quotes pre-build commands safely and patches the `ncbi/elasticblast-job-submit:4.1.0` build context to copy all templates and skip the GCP-style VolumeSnapshot step unless `ELB_CLOUD_PROVIDER=gcp`.
- `terminal/Dockerfile` and `terminal/profile.sh`: terminal image PATH/dependency setup now exposes the vendored `elastic-blast` CLI and installs the sibling runtime requirements.
- `terminal/patch_elastic_blast.py`: AKS workload templates are patched with `workload=blast` tolerations and node selectors for init, submit, batch, and vmtouch workloads.
- `docs/get-started.md`: updated with live validation evidence and the operational checks discovered during the run.
- `.gitignore`: keeps the Python packaging `lib/` ignore while explicitly allowing `web/src/lib/**`, which contains TypeScript source modules required by the production build.

## Validation evidence

- Clean Ubuntu 24.04 VM prerequisite replay succeeded: Azure CLI 2.86.0, azd 1.25.1, uv 0.11.14, Node v20.20.2, npm 10.8.2, jq 1.7, git 2.43.0, Python 3.12.13, `uv sync --all-groups`, and `npm ci`. A `Standard_B2s` validation VM was too small for the full backend test suite and exited with code 137, so the runbook now recommends `Standard_D4s_v5` for full clean-VM validation.
- Deployed health check against the active Container App succeeded after restoring the six-sidecar revision: `GET /api/health` returned `status=ok` on revision `ca-elb-control--0000040`.
- Runtime images built in ACR `acrelbnm5virmqrdi5c.azurecr.io`: `ncbi/elb:1.4.0`, `ncbi/elasticblast-job-submit:4.1.0`, `ncbi/elasticblast-query-split:0.1.4`, and `elb-openapi:4.9`.
- Storage private endpoints / DNS were repaired; `16S_ribosomal_RNA` preparation completed with 12 blobs and 18,433,197 bytes copied.
- AKS `elb-smoke-aks` provisioned in `rg-elb-ca` / `koreacentral`, Kubernetes 1.34.7, with `systempool` = `Standard_D2s_v3` x1 and `blastpool` = `Standard_D8s_v3` x1. Blob CSI was enabled and `azureblob-nfs-premium` existed.
- Kubernetes validation: `init-pv`, `submit-jobs`, `elb-finalizer`, and `blastn-batch-16s-ribosomal-rna-job-000` completed; workload pods ran on `blastpool` with `workload=blast` toleration and node selector.
- Result validation: downloaded `results/elb-smoke-16s-r3/job-6445053ac15a400d9e653b167013d929/batch_000-blastn-16S_ribosomal_RNA.out.gz` from the terminal sidecar; gzip size 1,971 bytes, decompressed XML size 17,918 bytes, and `<BlastOutput>` / `</BlastOutput>` were present.
- Targeted tests: `uv run pytest -q api/tests/test_azure_provision_aks.py api/tests/test_acr_build_task.py` -> 4 passed.
- Clean-clone reproducibility tests fixed during validation: the backend no longer depends on untracked `docs/temp` calibration output, and the frontend MSAL config no longer reads `window` in Node/Vitest imports.
- Targeted lint: `uv run ruff check api/tasks/azure.py api/tasks/acr.py api/services/image_tags.py api/tests/test_azure_provision_aks.py api/tests/test_acr_build_task.py terminal/patch_elastic_blast.py` -> all checks passed.
- Terminal patcher validation: `python3 -m py_compile terminal/patch_elastic_blast.py`; applied `terminal/patch_elastic_blast.py` twice to a temporary ElasticBLAST tree and confirmed workload tolerations/node selectors.
