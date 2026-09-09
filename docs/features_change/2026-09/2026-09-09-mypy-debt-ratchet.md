---
title: Production mypy debt ratchet
description: Make existing strict type-check debt explicit and prevent any unreviewed increase in CI.
tags: [contributor]
---

# Production mypy debt ratchet

## Motivation

The project declared strict [mypy](https://mypy.readthedocs.io/en/stable/)
checking for `api`, but CI did not run it. The first complete audit found 502
existing diagnostics across 97 non-test production files, so enabling a zero-error
gate immediately would either block every change or encourage broad suppressions.

## Change

- A checked-in baseline records diagnostics by production file and mypy error
  code.
- `check_mypy_baseline.py` fails on both increases and unreviewed reductions.
  Requiring a baseline refresh after reductions prevents later changes from
  consuming a previously freed error allowance.
- GitHub Actions and the isolated pre-push hook run the same ratchet.
- Tests are excluded from this production baseline; their dynamic fixtures
  remain covered by pytest.

This changes development validation only. Runtime packages and application
behavior are unchanged.

## Validation

- `uv run python scripts/dev/check_mypy_baseline.py` - baseline unchanged at
  502 diagnostics across 97 production files.
- `uv run pytest -q api/tests/test_mypy_baseline.py` - 4 passed.
- `shellcheck -x scripts/dev/git-hooks/pre-push` - passed.
