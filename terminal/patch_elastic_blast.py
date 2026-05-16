#!/usr/bin/env python3
"""Patch the vendored elastic-blast-azure clone for dashboard sharded runs."""

from __future__ import annotations

import sys
from pathlib import Path


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one match in {path}, found {count}")
    path.write_text(text.replace(old, new, 1))


def _replace_once_unless_present(
    path: Path, old: str, new: str, marker: str, *, allow_absent: bool = False
) -> None:
    text = path.read_text()
    if marker in text:
        return
    count = text.count(old)
    if count == 0 and allow_absent:
        return
    if count != 1:
        raise RuntimeError(f"expected one match in {path}, found {count}")
    path.write_text(text.replace(old, new, 1))


def patch_azure_py(root: Path) -> None:
    path = root / "src/elastic_blast/azure.py"
    _replace_once_unless_present(
        path,
        """        # Deploy finalizer\n        if self.auto_shutdown:\n            self._submit_finalizer_job()\n""",
        """        # Deploy finalizer. In partitioned/sharded mode this is also the\n        # result-merger and terminal marker writer, not just an auto-shutdown hook.\n        self._submit_finalizer_job()\n""",
        "result-merger and terminal marker writer",
        allow_absent=True,
    )
    _replace_once_unless_present(
        path,
        """            'ELB_DB_PARTITIONS': str(cfg.blast.db_partitions) if cfg.blast.db_partitions > 0 else '0',\n            'ELB_BLAST_PROGRAM': cfg.blast.program,\n""",
        """            'ELB_DB_PARTITIONS': str(cfg.blast.db_partitions) if cfg.blast.db_partitions > 0 else '0',\n            'ELB_BLAST_PROGRAM': cfg.blast.program,\n            'ELB_BLAST_OPTIONS': cfg.blast.options,\n""",
        "'ELB_BLAST_OPTIONS': cfg.blast.options",
    )


def patch_finalizer_template(root: Path) -> None:
    path = root / "src/elastic_blast/templates/elb-finalizer-aks.yaml.template"
    _replace_once_unless_present(
        path,
        """        - name: ELB_BLAST_PROGRAM\n          value: "${ELB_BLAST_PROGRAM}"\n        - name: BLAST_ELB_JOB_ID\n""",
        """        - name: ELB_BLAST_PROGRAM\n          value: "${ELB_BLAST_PROGRAM}"\n        - name: ELB_BLAST_OPTIONS\n          value: "${ELB_BLAST_OPTIONS}"\n        - name: BLAST_ELB_JOB_ID\n""",
        "name: ELB_BLAST_OPTIONS",
    )


def patch_finalizer_script(root: Path, merge_script_source: Path) -> None:
    path = root / "src/elastic_blast/templates/scripts/elb-finalizer-aks.sh"
    merge_script_target = path.parent / "merge-sharded-results.sh"
    merge_script_target.write_text(merge_script_source.read_text())

    text = path.read_text()
    if (
        "MERGE_OUTFMT=$(python3" in text
        and '"$MERGE_INPUT" "$MERGE_OUTPUT" "$MERGE_REPORT"' in text
    ):
        return
    if '"$MAX_HITS" "$MERGE_INPUT" "$MERGE_OUTPUT"' in text:
        raise RuntimeError(
            "elastic-blast-azure finalizer has the legacy tabular merge patch; "
            "update the cloned runtime to the XML-aware finalizer before building"
        )

    raise RuntimeError(
        "elastic-blast-azure finalizer is not XML-aware; update the cloned runtime "
        "before building the dashboard terminal image"
    )


def main() -> int:
    if len(sys.argv) not in {2, 3}:
        print(
            "usage: patch_elastic_blast.py /path/to/elastic-blast-azure [merge-script]",
            file=sys.stderr,
        )
        return 2
    root = Path(sys.argv[1]).resolve()
    merge_script_source = (
        Path(sys.argv[2]).resolve()
        if len(sys.argv) == 3
        else Path(__file__).with_name("merge-sharded-results.sh")
    )
    if not (root / "src/elastic_blast").is_dir():
        print(f"not an elastic-blast-azure source tree: {root}", file=sys.stderr)
        return 2
    if not merge_script_source.is_file():
        print(f"merge script not found: {merge_script_source}", file=sys.stderr)
        return 2

    patch_azure_py(root)
    patch_finalizer_template(root)
    patch_finalizer_script(root, merge_script_source)
    print("patched elastic-blast-azure finalizer for sharded result merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())