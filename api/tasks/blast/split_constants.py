"""Constants describing how split-mode BLAST parent/child jobs are tracked.

These values are referenced by [api/tasks/blast/__init__.py](./__init__.py)
when planning a split submission, summarising child status, and assembling
merge artifacts. They are pure data — no Azure SDK / Celery / Kubernetes
imports belong here.

Responsibility: Define the limits, status sets, and blob names used to plan
and reconcile split-mode BLAST jobs.
Edit boundaries: This module is for pure constants only. Anything that
imports `azure.*`, `kubernetes`, `celery`, or `redis` belongs elsewhere.
Key entry points: `STRICT_TIE_ORDER_MIN_TARGET_SEQS`,
`QUERY_FASTA_READ_MAX_BYTES`, `SPLIT_UPLOAD_VERIFY_BYTES`,
`SPLIT_CHILD_KNOWN_STATUSES`, `SPLIT_CHILD_CANCELLED_STATUSES`,
`SPLIT_CHILD_MERGED_RESULT_BLOB`, `SPLIT_CHILD_MERGE_REPORT_BLOB`,
`SPLIT_PARENT_MANIFEST_BLOB`, `SPLIT_MERGE_REPORT_MAX_BYTES`,
`SPLIT_CHILD_OPTION_ALLOWLIST`.
Risky contracts: `api/tests/test_blast_tasks.py` monkeypatches
`api.tasks.blast.QUERY_FASTA_READ_MAX_BYTES` and asserts on
`api.tasks.blast.SPLIT_MERGE_REPORT_MAX_BYTES` — those re-exports must keep
flowing through `api/tasks/blast/__init__.py`.
Validation: `uv run pytest -q api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

STRICT_TIE_ORDER_MIN_TARGET_SEQS = 5000
QUERY_FASTA_READ_MAX_BYTES = 100 * 1024 * 1024
SPLIT_UPLOAD_VERIFY_BYTES = 1024
SPLIT_CHILD_KNOWN_STATUSES = frozenset(
    {"queued", "running", "completed", "failed", "cancelled", "deleted"}
)
SPLIT_CHILD_CANCELLED_STATUSES = frozenset({"cancelled", "deleted"})
SPLIT_CHILD_MERGED_RESULT_BLOB = "merged_results.out.gz"
SPLIT_CHILD_MERGE_REPORT_BLOB = "merge-report.json"
SPLIT_PARENT_MANIFEST_BLOB = "split-results-manifest.json"
SPLIT_MERGE_REPORT_MAX_BYTES = 1024 * 1024
SPLIT_CHILD_OPTION_ALLOWLIST = frozenset(
    {
        "additional_options",
        "allow_approximate_sharding",
        "batch_len",
        "db_auto_partition",
        "db_effective_search_space",
        "db_partition_prefix",
        "db_partitions",
        "db_sharded",
        "db_total_bytes",
        "db_total_letters",
        "gap_extend",
        "gap_open",
        "is_inclusive",
        "machine_type",
        "max_target_seqs",
        "mem_limit",
        "mem_request",
        "num_nodes",
        "outfmt",
        "pd_size",
        "query_count",
        "query_effective_search_spaces",
        "shard_sets",
        "sharding_mode",
        "taxid",
        "tie_order_oracle_accessions",
        "tie_order_oracle_strict",
        "tie_order_oracle_text",
        "use_db_order_oracle",
        "word_size",
    }
)


__all__ = (
    "QUERY_FASTA_READ_MAX_BYTES",
    "SPLIT_CHILD_CANCELLED_STATUSES",
    "SPLIT_CHILD_KNOWN_STATUSES",
    "SPLIT_CHILD_MERGED_RESULT_BLOB",
    "SPLIT_CHILD_MERGE_REPORT_BLOB",
    "SPLIT_CHILD_OPTION_ALLOWLIST",
    "SPLIT_MERGE_REPORT_MAX_BYTES",
    "SPLIT_PARENT_MANIFEST_BLOB",
    "SPLIT_UPLOAD_VERIFY_BYTES",
    "STRICT_TIE_ORDER_MIN_TARGET_SEQS",
)
