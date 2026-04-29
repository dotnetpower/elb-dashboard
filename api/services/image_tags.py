"""Pinned ACR image tags consumed by ElasticBLAST on AKS.

Single source of truth in this repo. Update together with
`src/elastic_blast/constants.py` in the sibling `elastic-blast-azure` repo.
The monitoring UI highlights mismatches against this dict.
"""

from __future__ import annotations

IMAGE_TAGS: dict[str, str] = {
    "ncbi/elb": "1.4.0",
    "ncbi/elasticblast-job-submit": "4.1.0",
    "ncbi/elasticblast-query-split": "0.1.4",
}
