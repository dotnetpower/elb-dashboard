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
    "elb-openapi": "3.4",
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
        # Dockerfile.azure COPYs both files local to docker-job-submit/ and
        # templates/pvc-rwm-aks.yaml.template which lives at
        # src/elastic_blast/templates/. The upstream Makefile rsyncs the
        # templates into docker-job-submit/ and then runs `az acr build … .`
        # from inside docker-job-submit/, so the build context is the
        # subdirectory itself.
        #
        # For ACR Build Tasks we mirror that: source upload is the repo
        # root (so `cp -r src/elastic_blast/templates docker-job-submit/`
        # has access to both source and destination), but the actual
        # `docker build` step uses docker-job-submit/ as its context. ACR
        # Tasks scans the Dockerfile path relative to the source root
        # before the build step runs, so `dockerfile` must be the full
        # repo-relative path even when `build_context_dir` is set.
        "context": "",
        "dockerfile": "docker-job-submit/Dockerfile.azure",
        "build_context_dir": "docker-job-submit",
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
