"""Extract split-mode helpers + merge_split_results task into split_pipeline.py.

Reads api/tasks/blast/__init__.py, slices the two byte ranges, applies cross-
reference rewrites (bare-name -> _blast.X for symbols that stay in __init__.py),
and writes the result to api/tasks/blast/split_pipeline.py.
"""
from __future__ import annotations

import re
from pathlib import Path

INIT = Path("api/tasks/blast/__init__.py")
OUT = Path("api/tasks/blast/split_pipeline.py")

lines = INIT.read_text().splitlines(keepends=True)

split_helpers = lines[275:1331]  # L276..L1331 inclusive
merge_task = lines[2185:2231]  # L2186..L2231 inclusive

assert split_helpers[0].startswith("def _upload_split_query_files"), split_helpers[0]
assert merge_task[0].startswith(
    '@shared_task(name="api.tasks.blast.merge_split_results"'
), merge_task[0]

stay_names = [
    "_build_config_content",
    "_elastic_blast_argv",
    "_last_json",
    "_now_iso",
    "_progress",
    "_query_blob_path_from_query_file",
    "_relative_blob_path",
    "_result_error",
    "_retry_or_fail",
    "_snippet",
    "_submit_success_status",
    "_update_state",
]
# Intra-module split helpers that are also re-exported via __init__.py — route
# their call sites through _blast.X so tests can monkeypatch them on the
# package without having to also patch this submodule.
split_helpers = [
    "_aggregate_split_child_states",
    "_aggregate_split_merge_reports",
    "_build_parent_split_xml_result_bytes",
    "_build_split_child_submit_plan",
    "_child_state_payload",
    "_dispatch_split_child_submits",
    "_finalize_split_parent_results",
    "_iter_split_child_merged_result_chunks",
    "_load_split_child_merge_reports",
    "_parent_split_result_artifacts_present",
    "_parent_split_result_paths",
    "_read_split_child_merged_result_bytes",
    "_requires_split_parent_submission",
    "_result_blob_map",
    "_run_split_parent_submission",
    "_run_storage_query_split_parent_submission",
    "_split_child_options",
    "_split_child_result_paths",
    "_split_child_state_summary",
    "_upload_split_query_files",
    "_verify_split_child_result_artifacts",
    "_write_split_parent_result_artifacts",
]
stay_consts = ["STDOUT_SNIPPET_CHARS", "ELASTIC_BLAST_CFG_FILE"]


def apply_replacements(text: str) -> str:
    for name in stay_names + stay_consts + split_helpers:
        text = re.sub(r"(?<!\.)\b" + re.escape(name) + r"\b", "_blast." + name, text)
    # submit.apply_async / submit.delay -> _blast.submit.X
    text = re.sub(r"(?<!_blast\.)\bsubmit\.apply_async\b", "_blast.submit.apply_async", text)
    text = re.sub(r"(?<!_blast\.)\bsubmit\.delay\b", "_blast.submit.delay", text)
    # Revert _blast.X on def lines (function definitions themselves stay bare).
    text = re.sub(r"^def _blast\.", "def ", text, flags=re.MULTILINE)
    return text


split_replaced = apply_replacements("".join(split_helpers))
merge_replaced = apply_replacements("".join(merge_task))

HEADER = '''"""Split-mode query pipeline helpers and the merge Celery task.

Responsibility: Plan, dispatch, and finalize split-mode BLAST submissions —
upload child FASTA shards, fan out per-shard submits, aggregate child state,
verify result artifacts in Storage, and merge them into the parent result
blobs that the dashboard surfaces. The merge Celery task drives the final
parent-side finalization step.
Edit boundaries: Everything here is split-mode specific. Shared helpers
(snippets, state updates, config builders, elastic_blast argv, etc.) stay in
``api.tasks.blast`` and are reached through ``_blast.X`` so monkeypatch tests
on the package keep working. Storage URL helpers stay in ``api.tasks.blast``
for the same reason.
Key entry points:
  - ``_run_split_parent_submission`` /
    ``_run_storage_query_split_parent_submission`` (called by ``submit``).
  - ``_finalize_split_parent_results`` (called by ``check_status`` and the
    ``merge_split_results`` task).
  - ``merge_split_results`` (``@shared_task``
    ``name="api.tasks.blast.merge_split_results"``).
Risky contracts: Public task name must stay
``api.tasks.blast.merge_split_results``. Several helper names
(``_finalize_split_parent_results``, ``_run_split_parent_submission``,
``_aggregate_split_child_states``, ``_build_split_child_submit_plan``,
``_dispatch_split_child_submits``, ``_verify_split_child_result_artifacts``,
``_write_split_parent_result_artifacts``, ``_parent_split_result_paths``,
``_requires_split_parent_submission``, ``_upload_split_query_files``,
``_run_storage_query_split_parent_submission``) are re-exported from
``__init__.py`` so tests can patch them via ``blast._X``.
Validation: ``uv run pytest -q api/tests/test_blast_tasks.py``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from typing import Any

from celery import shared_task

from api.services.query_grouping import QuerySplitExecutionPlan
from api.tasks import blast as _blast
from api.tasks.blast.split_constants import (
    QUERY_FASTA_READ_MAX_BYTES,
    SPLIT_CHILD_CANCELLED_STATUSES,
    SPLIT_CHILD_KNOWN_STATUSES,
    SPLIT_CHILD_MERGE_REPORT_BLOB,
    SPLIT_CHILD_MERGED_RESULT_BLOB,
    SPLIT_CHILD_OPTION_ALLOWLIST,
    SPLIT_MERGE_REPORT_MAX_BYTES,
    SPLIT_PARENT_MANIFEST_BLOB,
    SPLIT_UPLOAD_VERIFY_BYTES,
)

LOGGER = logging.getLogger(__name__)

__all__ = (
    "_aggregate_split_child_states",
    "_aggregate_split_merge_reports",
    "_build_parent_split_xml_result_bytes",
    "_build_split_child_submit_plan",
    "_child_state_payload",
    "_dispatch_split_child_submits",
    "_finalize_split_parent_results",
    "_iter_split_child_merged_result_chunks",
    "_load_split_child_merge_reports",
    "_parent_split_result_artifacts_present",
    "_parent_split_result_paths",
    "_read_split_child_merged_result_bytes",
    "_requires_split_parent_submission",
    "_result_blob_map",
    "_run_split_parent_submission",
    "_run_storage_query_split_parent_submission",
    "_split_child_options",
    "_split_child_result_paths",
    "_split_child_state_summary",
    "_upload_split_query_files",
    "_verify_split_child_result_artifacts",
    "_write_split_parent_result_artifacts",
    "merge_split_results",
)


'''

OUT.write_text(HEADER + split_replaced + "\n" + merge_replaced)
print(f"Wrote {OUT}")
print(f"Lines: {sum(1 for _ in OUT.open())}")
