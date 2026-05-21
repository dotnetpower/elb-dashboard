# BLAST Submit JSON Cleanup

## Motivation

Warm node-local SSD runs were still spending roughly two minutes in the Submit Job step even after Stage DB was skipped. The ElasticBLAST CLI had already submitted Kubernetes work, but its normal post-submit cleanup hook could keep the terminal process open while the dashboard was already collecting job state and logs independently.

## User-facing change

Dashboard-driven JSON submit returns as soon as the ElasticBLAST submit path succeeds instead of waiting for the CLI cleanup log collector. Failed submits keep the existing cleanup behavior for diagnostics.

## API/IaC diff summary

No API contract or IaC changes. The terminal ElasticBLAST patch now clears the CLI cleanup stack only for successful Azure JSON submit mode. Host-mode local terminal execution also prepends a runtime override so the same behavior applies without editing the sibling `elastic-blast-azure` checkout.

## Validation evidence

- `uv run pytest -q api/tests/test_terminal_patch_elastic_blast.py`
- `uv run pytest -q api/tests/test_terminal_runtime_overrides.py`
