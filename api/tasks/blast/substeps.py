"""ElasticBLAST ``submit`` sub-step detection from CLI log lines.

Responsibility: Map a single ``elastic-blast submit`` stdout/stderr line to a
1..5 sub-progress checkpoint so the dashboard can render a stable mini-progress
signal during the ~2 minute submit window.
Edit boundaries: Pure parsing — no Celery, FastAPI, Azure SDK, or I/O. Add or
adjust patterns here when the upstream CLI changes its progress markers.
Key entry points: ``SUBMIT_SUBSTEP_TOTAL``, ``SUBMIT_SUBSTEP_PATTERNS``,
``detect_submit_substep``.
Risky contracts: Patterns are matched against raw CLI output; a regression in
upstream wording silently degrades the sub-progress bar but does not break
submit itself.
Validation: ``uv run pytest -q api/tests/test_blast_tasks.py``.
"""

from __future__ import annotations

import re
from typing import Any

SUBMIT_SUBSTEP_TOTAL = 5

SUBMIT_SUBSTEP_PATTERNS: tuple[tuple[re.Pattern[str], dict[str, Any]], ...] = (
    (
        re.compile(r"\[1/5\]\s+Writing configuration", re.IGNORECASE),
        {"index": 1, "label": "Writing configuration"},
    ),
    (
        re.compile(r"get_query_mode:\s*fsize=", re.IGNORECASE),
        {"index": 2, "label": "Analysing query mode"},
    ),
    (
        re.compile(r"Splitting queries into batches", re.IGNORECASE),
        {"index": 3, "label": "Splitting queries"},
    ),
    (
        re.compile(r"Upload workfiles", re.IGNORECASE),
        {"index": 4, "label": "Uploading workfiles"},
    ),
    (
        re.compile(r"Submitt(?:ing|ed) .*jobs?", re.IGNORECASE),
        {"index": 5, "label": "Submitting K8s jobs"},
    ),
)


def detect_submit_substep(line: str) -> dict[str, Any] | None:
    """Map a single ElasticBLAST submit log line to a (index, label) checkpoint.

    The upstream CLI prints a small set of yellow progress markers between
    submitting the config and dispatching K8s jobs. Surfacing them as a
    1/5..5/5 substep gives the dashboard a stable mini-progress signal for the
    ~2 min submit window.
    """
    if not line:
        return None
    for pattern, payload in SUBMIT_SUBSTEP_PATTERNS:
        if pattern.search(line):
            return {**payload, "total": SUBMIT_SUBSTEP_TOTAL}
    return None
