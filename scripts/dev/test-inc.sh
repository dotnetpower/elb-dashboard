#!/usr/bin/env bash
# Incremental test loop powered by pytest-testmon. Runs ONLY the tests whose
# covered code changed in your working tree since the last run, so the
# edit-test cycle stays sub-second instead of paying the full ~30 s suite.
#
# How it works:
#   - First invocation builds a per-host .testmondata coverage map (one full
#     run, so expect ~30-60 s the first time, or longer when scoped to the
#     whole suite). The DB is git-ignored.
#   - Every later invocation deselects unaffected tests automatically:
#     "changed files: N, ... / M deselected / K selected".
#
# Why this wrapper instead of `uv run pytest --testmon`:
#   - testmon SILENTLY disables selection when `-m` is passed, and pytest.ini's
#     addopts carry `-m "not slow and not subprocess"`. testmon is also
#     incompatible with pytest-xdist (`-n auto`, also in addopts). This wrapper
#     clears addopts (`-o addopts=`) so neither `-m` nor `-n` reaches testmon,
#     then re-adds the safety net (`--timeout=60`) by hand.
#   - Because `-m` is cleared, the first build includes the slow/subprocess
#     tests too; that is intentional for a complete dependency map. Subsequent
#     incremental runs still only touch affected tests.
#
# Usage:
#   scripts/dev/test-inc.sh                 # whole suite, incremental
#   scripts/dev/test-inc.sh api/tests/test_foo.py   # scope the map to one file
#   scripts/dev/test-inc.sh -k some_name    # extra pytest args pass through
#   ELB_TESTMON_RESET=1 scripts/dev/test-inc.sh     # rebuild the DB from scratch
#
# This is a LOCAL dev convenience only. CI and the pre-push hook still run the
# full `uv run pytest -q api/tests` (xdist + marker exclusion) — do not wire
# testmon into pytest.ini addopts or the CI workflows.
#
# Validation: run twice with no edits; the second run must report
# "0 selected" / "deselected" in well under a second.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

if [[ "${ELB_TESTMON_RESET:-}" == "1" ]]; then
  echo "test-inc: ELB_TESTMON_RESET=1 -> removing .testmondata" >&2
  rm -f .testmondata .testmondata-journal
fi

# Default scope is the whole api/tests tree; callers can override by passing
# explicit paths / pytest args. `-o addopts=` wipes the xdist + `-m` defaults
# that would otherwise disable testmon selection.
if [[ "$#" -eq 0 ]]; then
  set -- api/tests
fi

exec uv run pytest -o addopts= --testmon --timeout=60 --timeout-method=thread "$@"
