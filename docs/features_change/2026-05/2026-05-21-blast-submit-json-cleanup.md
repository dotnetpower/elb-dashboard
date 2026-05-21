# BLAST Submit Fast Azure Prep

## Motivation

Warm node-local SSD runs were still spending roughly two minutes in the Submit Job step even after Stage DB was skipped. The first cleanup-only hypothesis was incomplete: later job history showed the Kubernetes BLAST work finishing quickly while the ElasticBLAST CLI remained open during Azure Blob preflight and metadata writes.

## User-facing change

Dashboard-driven JSON submit now avoids two sources of orchestration delay:

- successful JSON submits clear the ElasticBLAST cleanup stack because the dashboard owns log/state collection;
- terminal-side ElasticBLAST Azure file checks, small reads, metadata writes, and database presence checks use the Azure Blob SDK fast path instead of starting `azcopy` for each tiny operation.

Failed submits keep the existing cleanup behavior for diagnostics, and the Azure SDK fast path falls back to ElasticBLAST's original helpers if an unexpected SDK error occurs.

## API/IaC diff summary

No API contract or IaC changes. The terminal ElasticBLAST patch still clears the CLI cleanup stack only for successful Azure JSON submit mode. Host-mode local terminal execution and the terminal sidecar now prepend a runtime override that patches ElasticBLAST Azure Blob helpers without editing the sibling `elastic-blast-azure` checkout.

## Validation evidence

- `uv run pytest -q api/tests/test_terminal_patch_elastic_blast.py`
- `uv run pytest -q api/tests/test_terminal_runtime_overrides.py`
- `uv run pytest -q api/tests/test_terminal_runtime_overrides.py api/tests/test_terminal_exec.py`
- `uv run ruff check terminal/runtime_overrides/sitecustomize.py terminal/exec_server.py api/tests/test_terminal_runtime_overrides.py`
- `bash -n scripts/dev/local-run.sh terminal/entrypoint.sh terminal/profile.sh`
