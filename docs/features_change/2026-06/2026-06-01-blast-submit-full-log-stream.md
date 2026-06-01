---
title: BLAST submit step now streams ElasticBLAST's full progress log
date: 2026-06-01
tags:
  - blast
  - logging
  - terminal
---

# BLAST submit step now streams ElasticBLAST's full progress log

## Motivation

During the **submit job / BLAST run** step the dashboard live log showed only a
handful of yellow markers (the `[1/5] Writing configuration`, `get_query_mode`,
`Splitting queries into batches`, `Upload workfiles` `print()` lines). Everything
else — query-splitting detail, batch counts, workfile upload progress, and the
critical `Submitting N jobs to cluster` lines — was missing, so the step looked
like it barely logged anything.

## Root cause

The control plane runs `elastic-blast submit --cfg elastic-blast.ini` with no
logging flags. Upstream ElasticBLAST defaults to:

- `logfile = elastic-blast.log` (a **file**, not stderr), and
- a stderr handler pinned to **WARNING and above**.

So all of ElasticBLAST's `logging.info(...)` progress output (the bulk of the
useful detail — `submit.py` alone has 13 logging calls, `azure.py` has 64) was
written to a file inside the **ephemeral terminal sidecar** that the dashboard
never reads. The live stream only captured the four `print()` markers that go to
stdout. The error path also only emitted minimal text for the same reason.

## User-facing change

The submit step's live console now shows ElasticBLAST's full `INFO`-level
progress log line-by-line (query splitting, batch counts, workfile uploads,
`Submitting N jobs to cluster`, …), giving real visibility into what the submit
is doing during the ~2 minute window.

Set `ELASTIC_BLAST_LOGLEVEL=DEBUG` (api/worker sidecar env) to crank verbosity up
to upstream's DEBUG default when diagnosing a stuck submit; `INFO` is the new
sensible default (upstream's raw DEBUG default is noisy with low-level SDK chatter).

## API / IaC diff summary

- [api/tasks/blast/cli_parsing.py](../../../api/tasks/blast/cli_parsing.py)
  - `_elastic_blast_argv()` now appends `--logfile stderr --loglevel <level>` so
    the full log streams to the terminal exec channel the dashboard already tails.
  - New `_elastic_blast_loglevel()` helper resolves the level from the
    `ELASTIC_BLAST_LOGLEVEL` env var (default `INFO`, validated against the
    allowed set, falls back to `INFO` on a bogus value).
  - `_result_error()` now takes the **tail** of stderr (new `_tail_snippet`)
    instead of the head, because the actionable failure message now sits after
    the INFO preamble.
- No IaC change required. The new env var is optional and defaults to `INFO`
  when unset, so the existing Container App template keeps working unchanged.

## Validation evidence

- `uv run ruff check api/tasks/blast/cli_parsing.py api/tests/test_blast_tasks.py` — clean.
- `uv run pytest -q api/tests/test_blast_tasks.py` — 124 passed (includes the
  updated `test_elastic_blast_argv_uses_cfg_file` and new
  `test_elastic_blast_loglevel_env_override`).
- `uv run pytest -q api/tests` — 2382 passed, 3 skipped (the lone transient
  `test_run_truncates_stdout_above_cap` timeout flake passes on its own re-run).
