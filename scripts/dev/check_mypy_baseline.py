#!/usr/bin/env python3
"""Enforce a ratcheting production-code mypy baseline.

Responsibility: Run strict mypy for non-test ``api`` modules and reject any
    file/error-code count that differs from the reviewed baseline.
Edit boundaries: Type-check orchestration and baseline serialization only; do
    not import application modules or alter mypy's diagnostics.
Key entry points: ``main``, ``parse_mypy_errors``, ``load_baseline``.
Risky contracts: Existing debt is tolerated only at the exact file/error-code
    counts recorded in ``mypy-baseline.txt``. Both increases and reductions fail
    until the baseline is reviewed and refreshed with ``--update``.
Validation: ``uv run pytest -q api/tests/test_mypy_baseline.py`` and
    ``uv run python scripts/dev/check_mypy_baseline.py``.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

from mypy import api as mypy_api

BASELINE_PATH = Path(__file__).with_name("mypy-baseline.txt")
_ERROR_RE = re.compile(r"^(?P<path>.+?\.py):\d+(?::\d+)?: error: .* \[(?P<code>[a-z-]+)\]$")


def parse_mypy_errors(output: str) -> Counter[tuple[str, str]]:
    """Count strict diagnostics by stable ``(path, error_code)`` key."""
    counts: Counter[tuple[str, str]] = Counter()
    for line in output.splitlines():
        match = _ERROR_RE.match(line)
        if match:
            module_path = match.group("path").replace("\\", "/")
            counts[(module_path, match.group("code"))] += 1
    return counts


def load_baseline(path: Path = BASELINE_PATH) -> Counter[tuple[str, str]]:
    """Load ``count path error-code`` records from the checked-in baseline."""
    counts: Counter[tuple[str, str]] = Counter()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3 or not parts[0].isdigit():
            raise ValueError(f"invalid mypy baseline line {line_number}: {raw_line!r}")
        count, module_path, error_code = parts
        key = (module_path, error_code)
        if key in counts:
            raise ValueError(f"duplicate mypy baseline entry on line {line_number}: {key!r}")
        counts[key] = int(count)
    return counts


def format_baseline(counts: Counter[tuple[str, str]]) -> str:
    """Render a deterministic baseline file."""
    lines = [
        "# Strict mypy debt ratchet for production api modules.",
        "# Format: <count> <path> <error-code>",
        "# Refresh only after reviewing every changed count:",
        "#   uv run python scripts/dev/check_mypy_baseline.py --update",
    ]
    lines.extend(
        f"{count} {module_path} {error_code}"
        for (module_path, error_code), count in sorted(counts.items())
    )
    return "\n".join(lines) + "\n"


def _run_mypy() -> tuple[Counter[tuple[str, str]], str, int]:
    stdout, stderr, exit_status = mypy_api.run(
        [
            "api",
            "--exclude",
            "^api/tests/",
            "--no-error-summary",
            "--show-error-codes",
            "--no-pretty",
            "--no-color-output",
        ]
    )
    return parse_mypy_errors(stdout), stderr, exit_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="Rewrite the baseline after reviewing the changed diagnostics.",
    )
    args = parser.parse_args(argv)

    current, stderr, exit_status = _run_mypy()
    if exit_status not in {0, 1}:
        if stderr:
            print(stderr, end="")
        print(f"mypy could not complete (exit={exit_status})")
        return exit_status
    if exit_status == 1 and not current:
        print(
            "mypy reported type errors, but the baseline parser recognized no diagnostics; "
            "refusing to pass with an unknown output format."
        )
        if stderr:
            print(stderr, end="")
        return 2

    if args.update:
        BASELINE_PATH.write_text(format_baseline(current), encoding="utf-8")
        print(
            f"Updated {BASELINE_PATH} with {sum(current.values())} errors "
            f"across {len({path for path, _code in current})} production files."
        )
        return 0

    baseline = load_baseline()
    if current == baseline:
        print(
            f"mypy baseline unchanged: {sum(current.values())} errors "
            f"across {len({path for path, _code in current})} production files."
        )
        return 0

    added = current - baseline
    removed = baseline - current
    print("mypy baseline changed; review the diagnostics before refreshing it.")
    for label, changes in (("added", added), ("removed", removed)):
        for (module_path, error_code), count in sorted(changes.items()):
            print(f"  {label}: {count} {module_path} [{error_code}]")
    print("Run with --update only after confirming every change is intentional.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
