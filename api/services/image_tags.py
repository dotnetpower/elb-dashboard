"""Pinned ACR image tags consumed by ElasticBLAST on AKS.

Responsibility: Pinned ACR image tags consumed by ElasticBLAST on AKS
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: Module import side effects and constants.
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

# ``elb-openapi`` tag uses the dashboard-specific ``4.x`` scheme (dashboard tracks
# the upstream FastAPI app's ``VERSION`` in commit messages: 4.14 == upstream
# 3.6.0 cache hardening; 4.15 == upstream 3.7.0 /v1/ready probe; 4.16 ==
# upstream 3.7.2 /v1/ready hardening — per-IP anonymous bucket, GC of empty
# rate buckets, optional stricter autoscaler-aware pool name match). Bump in
# lock-step with the sibling repo's ``docker-openapi/app/main.py`` ``VERSION``
# constant and record the mapping in the per-bump change note under
# ``docs/features_change/``.
IMAGE_TAGS: dict[str, str] = {
    "ncbi/elb": "1.4.0",
    "ncbi/elasticblast-job-submit": "4.1.0",
    "ncbi/elasticblast-query-split": "0.1.4",
    "elb-openapi": "4.16",
}

# GitHub source repo for ACR Build Tasks.
SOURCE_REPO = "https://github.com/dotnetpower/elastic-blast-azure.git"
SOURCE_BRANCH = "master"

# Build info per image: context subdirectory within the repo, Dockerfile path
# relative to the context. Image-name → build args mirror exactly what the
# upstream `make azure-build` recipes in
# https://github.com/dotnetpower/elastic-blast-azure invoke (see each
# `docker-XXX/Makefile` `az acr build -f Dockerfile.azure --image …`).
IMAGE_BUILD_INFO: dict[str, dict[str, str]] = {
    "ncbi/elb": {
        "context": "docker-blast",
        "dockerfile": "Dockerfile.azure",
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
        "pre_build_cmd": " && ".join(
            [
                "cp -r src/elastic_blast/templates docker-job-submit/",
                (
                    "sed -i 's|COPY templates/pvc-rwm-aks.yaml.template /templates/|"
                    "COPY templates/ /templates/|' docker-job-submit/Dockerfile.azure"
                ),
                (
                    r"sed -i 's|if ! $ELB_USE_LOCAL_SSD ; then|"
                    r"if ! $ELB_USE_LOCAL_SSD \&\& "
                    r"[ x${ELB_CLOUD_PROVIDER:-azure} = xgcp ] ; then|' "
                    "docker-job-submit/cloud-job-submit-aks.sh"
                ),
            ]
        ),
    },
    "ncbi/elasticblast-query-split": {
        "context": "docker-qs",
        "dockerfile": "Dockerfile.azure",
    },
    "elb-openapi": {
        "context": "docker-openapi",
        "dockerfile": "Dockerfile",
    },
}

