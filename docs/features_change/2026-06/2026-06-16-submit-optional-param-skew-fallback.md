---
title: Submit fallback for optional elastic-blast config-param version skew
description: When terminal elastic-blast rejects an optional dashboard config parameter as unrecognized, submit now strips only that optional hint and retries once instead of hard-failing the whole job.
tags:
  - blast
  - reliability
  - terminal
---

# Submit fallback for optional elastic-blast config-param version skew

## Motivation

A real submit failed with:

- `ERROR: Unrecognized configuration parameter "exp-skip-warmed-ssd-init" in section "cluster"`

That parameter is an optional optimization hint emitted by the dashboard when the warmup gate is ready. During a rolling deploy, api/worker can emit a new optional hint before the terminal toolchain base (which ships `elastic-blast`) is rebuilt, creating temporary version skew. Without a fallback, this turns an optional optimization into a hard submit outage.

## User-facing change

- `api.tasks.blast._stream_submit_command` now performs a one-time fallback:
  - first attempt runs with the generated config unchanged;
  - if `elastic-blast` fails with `Unrecognized configuration parameter "..."` and the name is in an allowlist of optional params (`exp-skip-warmed-ssd-init`), the submit retries once after removing only that optional line from the INI;
  - non-allowlisted params are never stripped, so required/invalid config errors still fail loudly.

## API/IaC diff summary

- Backend only. No route contract changes.
- No IaC changes.

## Validation evidence

- `uv run ruff check api/tasks/blast/submit_runtime.py api/tasks/blast/__init__.py api/tests/test_blast_tasks.py`: passed.
- `uv run pytest -q api/tests/test_blast_tasks.py -k "stream_submit_command or strip_optional or persist_submit_log"`: 6 passed.
- `uv run pytest -q api/tests/test_blast_tasks.py api/tests/test_blast_submit_capacity_gate.py api/tests/test_blast_config_sharding.py api/tests/test_blast_database_availability.py`: 217 passed.
- Live terminal verification on deployed revision `ca-elb-dashboard--0000027`: installed `elastic-blast` resolves from `/opt/elb/venv/bin/elastic-blast`, exposes `CFG_CLUSTER_EXP_SKIP_WARMED_SSD_INIT`, and accepts `exp-skip-warmed-ssd-init` in a dry-run config (only `az login` was missing inside the test shell).
