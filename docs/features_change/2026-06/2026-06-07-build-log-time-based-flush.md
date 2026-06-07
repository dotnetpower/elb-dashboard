---
title: Self-upgrade build logs flush incrementally during a build
description: BuildLogWriter now flushes on a short time interval so the live build-log viewer shows az acr build output mid-build instead of staying empty until the component finishes.
tags:
  - operate
  - release
---

# Self-upgrade build logs flush incrementally during a build (2026-06-07)

## Motivation

While running an in-app self-upgrade, the **Build logs** panel on the
Self-upgrade page showed `(empty)` for the entire duration of each
component's `az acr build`, then the full log appeared all at once when
that component finished.

Root cause: `BuildLogWriter.write_line` only flushed the in-process
buffer to the Append Blob when it reached **64 KiB**. A typical
`az acr build` stdout is well under 64 KiB (the live api build measured
~54 KiB), so the size trigger never fired and the buffer only drained in
the final `flush()` inside `image_builder.build`'s `finally` block. The
build-log viewer polls every ~3 s while the upgrade is active, but the
blob it reads stayed 0 bytes until the build completed — making the
"live" viewer useless during the build.

This was confirmed live: the `build-api.log` blob returned HTTP 200 with
length 0 for ~3 minutes during the api build, then jumped to 54,089
bytes at the instant the phase advanced to `az acr build frontend`.

This is **not** related to whether the latest code is deployed — it is a
pre-existing bug in the running revision's code.

## User-facing change

The Build logs panel now streams incremental `az acr build` output as the
build progresses, instead of staying empty until each component finishes.

## API / IaC diff summary

- `api/services/upgrade/build_logs.py`
  - Added `import time` and two named constants: `_FLUSH_SIZE_BYTES`
    (64 KiB, the existing size trigger, now named) and
    `_FLUSH_INTERVAL_SECONDS` (2.0 s, new time trigger).
  - `BuildLogWriter.__init__` records `_last_flush_monotonic`.
  - `write_line` now flushes when the buffer reaches the size threshold
    **or** when at least `_FLUSH_INTERVAL_SECONDS` have elapsed since the
    last successful flush.
  - `_flush_locked` updates `_last_flush_monotonic` only on a successful
    append; on failure it is left unchanged so the next `write_line`
    retries promptly. The existing buffer-restore-on-failure path is
    unchanged.
- No IaC change.

The append rate is bounded to roughly one Azure Blob append per 2 s
during continuous build output (≈150 appends for a 5-minute build), so
the live experience is restored without unbounded append spam.

## Validation evidence

- `uv run pytest -q api/tests/test_upgrade_build_logs.py api/tests/test_upgrade_image_builder.py` — 18 passed, including the new
  `test_writer_flushes_on_time_interval_below_size_threshold` (monkeypatches
  `time.monotonic` to prove a sub-64-KiB buffer flushes on the time trigger).
- `uv run pytest -q api/tests -k upgrade` — 260 passed.
- `uv run ruff check api/services/upgrade/build_logs.py api/tests/test_upgrade_build_logs.py` — clean.
- Live diagnosis via the dashboard's own `/api/upgrade/jobs/{job}/build-log/{component}`
  endpoint: blob 200 / 0 bytes during the api build, then 54,089 bytes at
  completion (the symptom this fix removes).
