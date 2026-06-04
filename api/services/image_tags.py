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
# upstream 3.7.2 /v1/ready hardening; 4.17 == upstream 3.7.3 /v1/ready
# critique-fix round — X-Forwarded-For-aware anonymous bucket, LRU-bounded
# rate-bucket dict, exact-match autoscaler pool name parser; 4.18 == 4.17 app
# code REBUILT FROM THE PATCHED LOCAL CONTEXT to restore the core_nt sharding
# translation that 4.17 silently dropped — see
# docs/features_change/2026-06/2026-06-02-openapi-resharding-regression-fix.md;
# 4.19 == upstream 3.7.4 — Mode B /v1/jobs request examples now use a real
# E. coli K-12 16S rRNA query (NR_024570.1) against 16S_ribosomal_RNA with
# taxid 562 and outfmt 5, replacing the biologically nonsensical Monkeypox
# ATGC-repeat placeholders — see
# docs/features_change/2026-06/2026-06-04-openapi-mode-b-16s-example-fix.md;
# 4.20 == upstream 3.7.5 — _refresh_job_status now gates the SUCCESS.txt
# marker on _list_result_files so a job only reports completed once the
# result listing the download path uses is populated, with a bounded
# RESULTS_VISIBILITY_GRACE_SECONDS fallback; fixes the completed -> /results
# 404 race from Azure Blob list-after-write visibility lag — see
# docs/features_change/2026-06/2026-06-04-openapi-results-visibility-race.md).
# Bump in lock-step with the sibling repo's ``docker-openapi/app/main.py``
# ``VERSION`` constant and record the mapping in the per-bump change note under
# ``docs/features_change/``.
#
# IMPORTANT: the ``elb-openapi`` image MUST be built from the dashboard-patched
# local sibling context (run ``scripts/dev/patch-openapi-build-context.py
# ~/dev/elastic-blast-azure/docker-openapi`` THEN ``az acr build … docker-openapi``).
# A raw GitHub-master build omits the core_nt sharding translation and the
# patched ElasticBLAST runtime — that omission is exactly the 4.17 regression
# the 2026-06-02 note documents.
#
# Rollout order (charter): build+push the sibling image to ACR FIRST, then
# move the pin here. See docs/features_change/2026-05/2026-05-29-openapi-critique-fixes.md
# "Rollout order" for the safe procedure. The 2026-05-30 P0 rollback exists
# because this order was inverted on 2026-05-29.
IMAGE_TAGS: dict[str, str] = {
    "ncbi/elb": "1.4.0",
    "ncbi/elasticblast-job-submit": "4.1.0",
    "ncbi/elasticblast-query-split": "0.1.4",
    "elb-openapi": "4.20",
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

