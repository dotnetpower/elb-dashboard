"""Pinned ACR image tags consumed by ElasticBLAST on AKS.

Responsibility: Pinned ACR image tags consumed by ElasticBLAST on AKS
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: Module import side effects and constants.
Risky contracts: elb-openapi builds must use the dashboard source, verify the
pinned sibling commit, and run the local build-context patcher before Docker.
Validation: `uv run pytest -q api/tests/test_acr_build_task.py
api/tests/test_patch_openapi_build_context.py`.
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
# docs/features_change/2026-06/2026-06-04-openapi-results-visibility-race.md;
# 4.21 == 4.20 app code (upstream 3.7.5, unchanged) REBUILT FROM THE PATCHED
# LOCAL CONTEXT to pick up the sharded outfmt 7 support that 4.20 predates:
# the partitioned-outfmt gate widening (allow ``7``/``7 std``), the quote-safe
# multi-token ``-outfmt`` argv rebuild in blast-run-aks.sh, and the field-aware
# shard merge. 4.20 was pinned 2026-06-04, before those patches landed
# (2026-06-10), so an OpenAPI ``-outfmt 7 std staxids`` submit failed with
# "7 is not supported for merge" until this rebuild — see
# docs/features_change/2026-06/2026-06-10-openapi-outfmt7-gate-rebuild.md).
# 4.24 == upstream 3.7.6 — sibling external-payload hardening: ``blast_version``
# falls back to the pinned ElasticBLAST release BLAST+ version (``2.17.0+``)
# when the binary probe fails and ``ELB_BLAST_VERSION`` is unset (fixes #9);
# ``db_version_detail.detail`` is now a dict (was a ``json.dumps`` string,
# fixes #10); every natural terminal transition in ``_refresh_job_status``
# snapshots the final ``k8s_summary`` (fixes #18) AND emits a best-effort
# webhook to ``CONTROL_PLANE_URL`` so the dashboard sees completed/failed
# without waiting for the next sync cycle (fixes #16/#17). Cancel/stuck path
# is unchanged — ``_cancel_job`` already notifies, so no double-notify. See
# docs/features_change/2026-06/2026-06-14-openapi-external-payload-hardening.md.
# 4.25 stages the BLAST v5 seqid->taxid filter index ``.nos``/``.not`` in the
# shard download pattern so sharded core_nt searches with a taxonomy
# include/exclude filter stop failing with blastn exit 255. 4.26 adds the
# self-heal that invalidates a pre-fix ``.download-complete`` warm cache missing
# that index so already-warmed clusters re-stage it on the next warmup. See
# docs/features_change/2026-06/2026-06-20-sharded-negative-taxids-not-nos-fix.md.
# 4.27 == sibling watchdog now reclaims a dispatching/submitting job whose
# in-process submit thread died (pod restart after the cluster was stopped
# mid-submit) within one watchdog tick instead of after SUBMIT_STUCK_SECONDS
# (2h), so post-stop/start zombies stop wedging the MAX_ACTIVE dispatcher
# (fixes #62). Bounded by ELB_OPENAPI_SUBMIT_MAX_RETRIES; an alive (cold-staging)
# submit thread is never touched. See
# docs/features_change/2026-06/2026-06-21-openapi-dead-thread-slot-reclaim.md.
# 4.28 rebuilds the same sibling app/runtime from verified sibling commit
# 352a1f4. Live 4.27 generated an unsharded full-core_nt config even when the
# Service Bus producer sent resource_profile=core_nt_safe. The verified 4.28
# context contains both the app-side core_nt profile translation and the
# Dockerfile runtime patch hook; ACR run de4n produced digest sha256:210d0103….
# 4.29/4.30 were intermediate incident builds. 4.31 rebuilds sibling commit
# 352a1f4 after the final retry review raised the bounded transient budget to
# six attempts and closed the remaining monitoring GET retry paths. 4.34 uses
# the same verified sibling commit and ElasticBLAST source pin, with
# content-aware reconciliation for the image-installed elb-scripts ConfigMap.
# 4.35 adds a 900 KiB size bound and post-apply content verification. 4.38 adds
# exact DB-order oracle attachment, immutable generation-path + search-space
# pinning, explicit soft masking, and full-DB XML statistics recalibration.
# 4.39 preserves one validated query-specific search space instead of replacing
# it with the 64-nt active-generation fallback; missing values still receive
# the active fallback and malformed/duplicate values fail closed. ACR run de7w
# produced digest sha256:6afca07b9132a843877f1a49b2baa36b4d7da303b325f41c4741c5c533062a7a.
# 4.40 fixes immutable-generation init URLs: active shard prefixes resolve the
# generation's full DB root, the container-root metadata blob, and an expected
# source version derived from the immutable path. ACR run de80 produced digest
# sha256:7e5f7401f40b05177a93ca94e900a8636f5cc4560da6e4dd9aa99e877ecccc53.
# 4.41 corrects the immutable payload root itself: the generation directory
# contains `core_nt.*` files directly. ACR run de83 produced digest
# sha256:01c400629c0976873026dc91aa5e7b05e5e626ffdef1d20efd1cc6a69072b3eb.
# 4.42 isolates each finalizer oracle-part azcopy process from the surrounding
# URL-manifest stdin, so all ten parts download instead of part 0 consuming the
# remaining lines. ACR run de86 produced digest
# sha256:3c43d992468f6e093ecbc5fff5f93047c079e6e7afb7408f1b29a95182e0fb62.
# 4.43 requires oracle-v2 and consumes shard/local-OID/accession rows so every
# grouped alias shares the sequence's exact rank while OID resets across shards
# remain distinct. ACR run de89 produced digest
# sha256:e76e25509f60269116be7ad5cc99e3254c4594d952f4eed00ae0ac9bd458956c.
# 4.44 exposes the canonical merged result after partition finalization and
# re-lists a pre-finalizer shard-only cache until that artifact appears. ACR run
# de8e produced digest
# sha256:d6e21281d4bddd5969cbedc59daad48d332238bb439c8239509f0af4fcf9c9ee.
# 4.45 permits that exact canonical basename through the result-download path
# guard while preserving traversal and arbitrary-file rejection. ACR run de8f
# produced digest
# sha256:9aafa0767fcc3372325895dfd3dc5c9416a6a61aa5257271e85793e4a9bd4e79.
# 4.46 reproduces Web BLAST taxonomy-filtered statistics: a validated request
# context supplies filtered DB counts, the native BLAST -dbsize value, the
# distinct HSP scoring -searchsp, and canonical result statistics. The private
# context manifest is immutable across idempotent replays and the finalizer
# fails closed when the runtime flags or active generation disagree.
# 4.47 adds an isolated disk-backed one-shard topology for validated Web BLAST
# statistical contexts. Other precise requests retain their parallel shard
# count, and runtime evidence records topology, filter semantics, and candidate
# budget before the dashboard may project an exactness claim.
# 4.48 makes tabular shard merging disk-backed: long qseq/sseq rows and rank
# metadata spool through SQLite instead of growing the finalizer Python heap.
# It also carries a widened strict-oracle candidate pool separately from the
# requested final result cap. ACR run de8p produced digest
# sha256:134827a3c63ea6caa57a6a3a036cb87c6c5b4aff515be6dff6c9174374c9bdbc.
# 4.49 streams the multi-gigabyte full-DB order oracle and retains only ranks
# for subjects present in the shard candidate pool. This removes the remaining
# 26-40 GiB finalizer heap growth that 4.48's SQLite hit spool did not address.
# ACR run de8q produced digest
# sha256:d480c951ab83238ad362fe916a2a72f583dc20f43795222b8f0704f152822eb5.
# 4.50 preserves the legacy fail-safe behavior for query-oracle tabular output
# that has no subject accession column and is the final image built from merger
# SHA-256 c362535f0f85b0982cba43c0d48a0e82b63fce78422fb21a816e18685e513c52.
# ACR run de8r produced digest
# sha256:4d837a0fab027242df118ddce776df07fa0e0657e70adfbc15dee2c537927f5e.
# 4.51 restores server-owned search-space derivation for direct precise
# core_nt `/v1/jobs` requests. It removes the obsolete pre-resolution HTTP 400
# guard while preserving explicit query-specific values and fail-closed active
# generation validation.
# 4.52 preserves 4.51 behavior and rejects noncanonical workflow correlation
# values as ElasticBLAST runtime IDs so terminal webhooks carry the real
# `job-<32hex>` identity before pod-log TTL cleanup. ACR run de9a produced
# digest sha256:73bda8e52b754deac8558398a247eb8fe3fb5b48243413e6e8d543e73318f83f.
# 4.53 was the first result-readiness validation image. Live inspection found
# that its inherited 120-second visibility fallback could still complete a
# partitioned job without the canonical merge. 4.54 makes that path fail closed:
# the durable success marker and merged artifact are both required, and a
# missing merge becomes finalizer_failed at the bounded 30-minute deadline. ACR
# run de9n produced digest
# sha256:d697254ca259840c76006efc30ddf2dee447c30857038906d9d03498cbd5f26b.
# Tag 4.52 remains the pre-change rollback boundary; 4.53 remains diagnostic.
# 4.55 adds the authenticated RID reference-context resolver, typed readiness
# response models, hardened bounded XML evidence parsing, and explicit
# result-selection policy examples while retaining 4.54 as the rollback image.
# 4.56 sources those runtime contracts plus opt-in sequence-diversity result
# selection natively from sibling commit 6132ccb. It accepts a valid zero
# length adjustment, rejects unsupported policy combinations without fallback,
# and keeps each explicit candidate pool as a finite per-request shard bound.
# 4.57 adds job-scoped candidate-order oracles, immutable-generation SSD cache
# attestation, and runtime-scoped success markers. Its live rollout exposed a
# restart replay defect, so it remains diagnostic while 4.56 is the rollback
# boundary. ACR run de9y produced digest
# sha256:9f8fc4aa59c552cd77681df445a3736056f655d6b8be553f43aafd42a52a92fb.
# 4.58 persists a deterministic runtime ID before submit, recovers legacy
# in-flight runtime IDs from Kubernetes, fails closed when observation fails,
# and invokes ElasticBLAST's JSON idempotency path. ACR run dea0 produced digest
# sha256:91db00630b0f9f753bb3c28fd05a3eea597b14646e56e1dbc56c6a6dc59494cc.
# 4.59 makes recovery observation tri-state, requires the runtime ID ConfigMap
# write before submit side effects, terminates in-memory on persistence failure,
# and preserves the bounded submit deadline. ACR run dea6 produced digest
# sha256:c24807bc7aaf9e144054301936caa35addba15421304d346fdb4bedefa59f8d4.
# 4.60 globally stable-sorts candidate rows by numeric shard/local OID across
# query batches and verifies the sorted row count before enabling the fast path.
# ACR run dea7 produced digest
# sha256:9559103f3d448f3e19535ef11bd59da6fb2900cb9b3770c2356c0c1c2623a961.
# 4.36/4.37 were intermediate builds and were never deployed. Tags 4.32
# and 4.33 were older June builds, so the rollout intentionally skipped them
# rather than overwriting an existing rollback boundary. ACR run de5f produced
# digest sha256:7bac4202…581fbf4.
# The dashboard's
# terminal/patch_elastic_blast.py layer adds bounded replay-safe kubectl retries
# and ttlSecondsAfterFinished; the build context asserts that source/system/venv
# batch templates all carry the TTL. ACR run de53 produced digest sha256:ed8b67d7….
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
    "elb-openapi": "4.60",
}

# GitHub source repo for ACR Build Tasks.
SOURCE_REPO = "https://github.com/dotnetpower/elastic-blast-azure.git"
SOURCE_BRANCH = "master"
DASHBOARD_SOURCE_REPO = "https://github.com/dotnetpower/elb-dashboard.git"
OPENAPI_SIBLING_SOURCE_REF = "6132ccba35714c77ee724642697674ec3cf975e3"

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
        # Clone one reviewed sibling commit and reapply the dashboard patcher
        # idempotently as a compatibility/safety assertion. Since sibling
        # commit 6132ccb, the hardened runtime contracts are native; the patcher must
        # produce no semantic drift. ACR's
        # implicit cmd-step image does not contain git, so pin an explicit
        # tool image that carries git + Python + grep + bash.
        "source_repo": DASHBOARD_SOURCE_REPO,
        "context": ".acr-openapi/docker-openapi",
        "dockerfile": "Dockerfile",
        "timeout_seconds": "1200",
        "pre_build_image": (
            "python:3.12-bookworm@"
            "sha256:581429e3df12d76e6af4be5ab7d0e7fc2013eb57dc23d2de691411c8efdbb970"
        ),
        "pre_build_cmd": " && ".join(
            [
                "rm -rf .acr-openapi",
                "git init .acr-openapi",
                (
                    "git -C .acr-openapi remote add origin "
                    "https://github.com/dotnetpower/elastic-blast-azure.git"
                ),
                (f"git -C .acr-openapi fetch --depth 1 origin {OPENAPI_SIBLING_SOURCE_REF}"),
                "git -C .acr-openapi checkout --detach FETCH_HEAD",
                (f'test "$(git -C .acr-openapi rev-parse HEAD)" = "{OPENAPI_SIBLING_SOURCE_REF}"'),
                (
                    "grep -q 'def _replace_stale_core_nt_search_space_fallback' "
                    "scripts/dev/patch-openapi-build-context.py"
                ),
                ("grep -q 'def read_active_database' scripts/dev/openapi-overlays/exact_oracle.py"),
                ("python3 scripts/dev/patch-openapi-build-context.py .acr-openapi/docker-openapi"),
            ]
        ),
    },
}
