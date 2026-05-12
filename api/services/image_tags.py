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
    "elb-openapi": "2.0",
}

# GitHub source repo for ACR Build Tasks.
SOURCE_REPO = "https://github.com/dotnetpower/elastic-blast-azure.git"
SOURCE_BRANCH = "master"

# Build info per image: context subdirectory within the repo, Dockerfile path
# relative to the context.
IMAGE_BUILD_INFO: dict[str, dict[str, str]] = {
    "ncbi/elb": {
        "context": "docker-blast",
        "dockerfile": "Dockerfile",
    },
    "ncbi/elasticblast-job-submit": {
        # Dockerfile.azure COPYs templates/pvc-rwm-aks.yaml.template which
        # lives at src/elastic_blast/templates/. We use the repo root as
        # context and the Dockerfile path relative to root. The Makefile's
        # pre_cmd rsync copies templates into docker-job-submit/templates/.
        # For ACR Build, we use a special context that includes both dirs.
        "context": "",
        "dockerfile": "docker-job-submit/Dockerfile.azure",
        "pre_build_cmd": "cp -r src/elastic_blast/templates docker-job-submit/",
    },
    "ncbi/elasticblast-query-split": {
        "context": "docker-qs",
        "dockerfile": "Dockerfile",
    },
    "elb-openapi": {
        "context": "docker-openapi",
        "dockerfile": "Dockerfile",
    },
}
