#!/usr/bin/env python3
"""Compare full BLAST tabular output with merged shard output.

This harness intentionally operates on local outfmt 6/std files so precision
work can be validated without launching AKS. Generate full and shard outputs
with the same BLAST+ version/options, then run this script to exercise the same
merge engine used by the terminal finalizer.
"""

from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MERGE_SCRIPT = ROOT / "terminal" / "merge-sharded-results.sh"


def _read_rows(path: Path) -> list[str]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as handle:  # type: ignore[arg-type]
        return [line.rstrip("\n") for line in handle if line.strip() and not line.startswith("#")]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", required=True, type=Path, help="full-DB outfmt 6/std file")
    parser.add_argument(
        "--shard",
        required=True,
        action="append",
        type=Path,
        help="shard outfmt 6/std file; pass once per shard output",
    )
    parser.add_argument("--max-target-seqs", required=True, type=int)
    parser.add_argument("--json", type=Path, help="optional JSON report output")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        merge_input = temp / "all-shard-hits.tsv"
        merged_gz = temp / "merged.out.gz"
        merge_report = temp / "merge-report.json"
        with merge_input.open("w") as output:
            for shard_path in args.shard:
                for row in _read_rows(shard_path):
                    output.write(row + "\n")

        subprocess.run(
            [
                "bash",
                str(MERGE_SCRIPT),
                str(merge_input),
                str(merged_gz),
                str(merge_report),
                str(len(args.shard)),
                "blastn",
                f"-outfmt 6 -max_target_seqs {args.max_target_seqs}",
            ],
            check=True,
        )

        full_rows = _read_rows(args.full)
        merged_rows = _read_rows(merged_gz)
        report = json.loads(merge_report.read_text())
        result = {
            "exact_ordered_rows_equal": full_rows == merged_rows,
            "exact_line_sets_equal": sorted(full_rows) == sorted(merged_rows),
            "full_rows": len(full_rows),
            "merged_rows": len(merged_rows),
            "merge_report": report,
        }
        if args.json:
            args.json.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["exact_line_sets_equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())