"""Unit tests for the AKS-fanout prepare-db Job / ConfigMap builders.

Responsibility: Lock the K8s manifest shape that Phase 1 ships so
    accidental edits (wrong API version, dropped TTL, removed downward-API
    env, weaker tolerations) are caught before the cluster ever sees them.
Edit boundaries: Pure unit tests over `api.services.k8s.prepare_db_jobs`.
    The Celery task / route path is exercised in their own test files.
Key entry points: `test_manifest_indexed_completion`,
    `test_manifest_env_includes_downward_api`,
    `test_job_name_is_deterministic_and_safe`.
Risky contracts: Manifest shape drives the cluster reconciliation loop;
    weakening `completionMode: Indexed` or `ttlSecondsAfterFinished`
    would resurrect zombie-pod / non-Indexed regressions. Issue #7
    acceptance #6 requires TTL 3600s.
Validation: `uv run pytest -q api/tests/test_prepare_db_aks_manifest.py`.
"""

from __future__ import annotations

import pytest
from api.services.k8s.prepare_db_jobs import (
    DEFAULT_ACTIVE_DEADLINE_SECONDS,
    DEFAULT_AZCOPY_IMAGE,
    DEFAULT_TTL_SECONDS_AFTER_FINISHED,
    PREPARE_DB_AKS_SCRIPT,
    SOURCE_VERSION_ANNOTATION,
    build_prepare_db_job_manifest,
    build_prepare_db_scripts_configmap,
    prepare_db_job_name,
)


def _baseline_manifest(**overrides):
    defaults: dict = dict(
        job_name="prepare-db-corent-202605210105",
        db_name="core_nt",
        storage_account="stelbtest001",
        source_version="2026-05-21-01-05-02",
        shard_count=4,
        scripts_configmap="prepare-db-corent-202605210105",
    )
    defaults.update(overrides)
    return build_prepare_db_job_manifest(**defaults)


def test_job_name_is_deterministic_and_safe() -> None:
    name = prepare_db_job_name("core_nt", "2026-05-21-01-05-02")
    assert name == prepare_db_job_name("core_nt", "2026-05-21-01-05-02")
    assert len(name) <= 52
    # K8s name regex: starts with lowercase alnum, only [a-z0-9-]
    import re

    assert re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", name)
    # Version segment must come from source_version digits.
    digit_fragments = ("260521010502", "210105022", "01050200", "0521010502")
    assert any(frag in name for frag in digit_fragments)
    # db_name fragment present
    assert "core" in name or "core-nt" in name


def test_job_name_handles_weird_characters() -> None:
    name = prepare_db_job_name("My_Weird DB!", "")
    import re

    assert re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", name)


def test_manifest_indexed_completion() -> None:
    manifest = _baseline_manifest()
    assert manifest["apiVersion"] == "batch/v1"
    assert manifest["kind"] == "Job"
    spec = manifest["spec"]
    assert spec["completionMode"] == "Indexed"
    assert spec["completions"] == 4
    assert spec["parallelism"] == 4


def test_manifest_safety_fields() -> None:
    manifest = _baseline_manifest()
    spec = manifest["spec"]
    assert spec["ttlSecondsAfterFinished"] == DEFAULT_TTL_SECONDS_AFTER_FINISHED
    assert spec["ttlSecondsAfterFinished"] == 3600  # issue #7 acceptance #6
    assert spec["activeDeadlineSeconds"] >= 60
    # Per-INDEX backoff so a transient blip in one shard never trips a global
    # budget and kills every in-flight pod (the unconverging retry loop). The
    # global `backoffLimit` is intentionally omitted: K8s defaults it to
    # MaxInt32 when `backoffLimitPerIndex` is set.
    assert "backoffLimit" not in spec
    assert spec["backoffLimitPerIndex"] >= 0
    assert spec["backoffLimitPerIndex"] <= 5  # don't burn budget retrying forever
    # `maxFailedIndexes == shard_count` keeps healthy shards running to
    # completion even if a broken shard exhausts its retries.
    assert spec["maxFailedIndexes"] == spec["completions"]


def test_manifest_env_includes_downward_api() -> None:
    manifest = _baseline_manifest()
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    env_by_name = {e["name"]: e for e in container["env"]}
    # The script reads ${JOB_COMPLETION_INDEX} — must come from kubelet.
    assert "JOB_COMPLETION_INDEX" in env_by_name
    field_path = env_by_name["JOB_COMPLETION_INDEX"]["valueFrom"]["fieldRef"]["fieldPath"]
    assert "batch.kubernetes.io/job-completion-index" in field_path
    assert env_by_name["ELB_DB_NAME"]["value"] == "core_nt"
    assert env_by_name["ELB_STORAGE_ACCOUNT"]["value"] == "stelbtest001"


def test_manifest_pod_safety() -> None:
    manifest = _baseline_manifest()
    pod_spec = manifest["spec"]["template"]["spec"]
    assert pod_spec["restartPolicy"] == "Never"
    tols = pod_spec["tolerations"]
    assert any(t.get("key") == "workload" and t.get("value") == "blast" for t in tols)


def test_manifest_annotations_carry_source_version() -> None:
    manifest = _baseline_manifest()
    annotations = manifest["metadata"]["annotations"]
    assert annotations.get(SOURCE_VERSION_ANNOTATION) == "2026-05-21-01-05-02"


def test_invalid_db_name_raises() -> None:
    with pytest.raises(ValueError):
        _baseline_manifest(db_name="bad name!")


def test_invalid_storage_account_raises() -> None:
    with pytest.raises(ValueError):
        _baseline_manifest(storage_account="UPPER_invalid")


def test_invalid_image_raises() -> None:
    with pytest.raises(ValueError):
        _baseline_manifest(image="bad image with spaces")


def test_invalid_shard_count_raises() -> None:
    with pytest.raises(ValueError):
        _baseline_manifest(shard_count=0)


def test_invalid_ttl_raises() -> None:
    with pytest.raises(ValueError):
        _baseline_manifest(ttl_seconds_after_finished=10)


def test_configmap_has_script_and_shard_files() -> None:
    cm = build_prepare_db_scripts_configmap(
        shards=[["a.nhr", "b.nhr"], ["c.nhr"]],
        name="prepare-db-corent-202605210105",
    )
    assert cm["apiVersion"] == "v1"
    assert cm["kind"] == "ConfigMap"
    assert cm["data"]["prepare-db.sh"] == PREPARE_DB_AKS_SCRIPT
    assert cm["data"]["shard-00.txt"] == "a.nhr\nb.nhr\n"
    assert cm["data"]["shard-01.txt"] == "c.nhr\n"


def test_configmap_rejects_empty_shards() -> None:
    with pytest.raises(ValueError):
        build_prepare_db_scripts_configmap(shards=[], name="prepare-db-corent-x")


def test_configmap_rejects_bad_name() -> None:
    with pytest.raises(ValueError):
        build_prepare_db_scripts_configmap(shards=[["a"]], name="Bad Name")


def test_manifest_volumes_include_scripts_and_azcopy_cache_only() -> None:
    """Phase 1.5: the PipeBlob refactor removed the 2 GiB tmpfs `/tmp`
    volume because nothing on disk is staged anymore. Only `scripts` (the
    ConfigMap) and `azcopy-cache` (small tmpfs for `~/.azcopy`) survive."""
    manifest = _baseline_manifest()
    pod_spec = manifest["spec"]["template"]["spec"]
    volumes = pod_spec["volumes"]
    by_name = {v["name"]: v for v in volumes}
    assert set(by_name) == {"scripts", "azcopy-cache"}, by_name
    assert by_name["scripts"]["configMap"]["name"] == "prepare-db-corent-202605210105"
    assert by_name["scripts"]["configMap"]["defaultMode"] == 0o755
    assert by_name["azcopy-cache"]["emptyDir"]["medium"] == "Memory"
    # Shrunk from 128Mi to 64Mi — PipeBlob mode does not write plan files,
    # so we don't need the bigger reservation.
    assert by_name["azcopy-cache"]["emptyDir"]["sizeLimit"] == "64Mi"
    # And the matching volumeMount is the only one besides scripts.
    container = pod_spec["containers"][0]
    mount_names = {m["name"] for m in container["volumeMounts"]}
    assert mount_names == {"scripts", "azcopy-cache"}


def test_manifest_default_image_is_pinned() -> None:
    """Charter §3: pin Azure CLI >= 2.81. `:latest` is forbidden."""
    assert DEFAULT_AZCOPY_IMAGE == "mcr.microsoft.com/azure-cli:2.81.0"
    manifest = _baseline_manifest()
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == DEFAULT_AZCOPY_IMAGE
    # Pinned tag => `IfNotPresent` is safe and avoids re-pulling on every
    # backoff retry. With `:latest` this would have been racy.
    assert container["imagePullPolicy"] == "IfNotPresent"


def test_manifest_default_active_deadline_is_4_hours() -> None:
    """`nt`/`core_nt` stream for well over an hour at 10-shard parallelism
    (the dashboard badges them "May take hours"). The old 45 min ceiling
    fired `activeDeadlineSeconds` mid-download, marking the Job
    `Failed/DeadlineExceeded` and surfacing abandoned-but-not-errored files
    as a misleading "partial · N failed". 4h stops cutting the big DBs off;
    normal completion still exits the instant all shards succeed."""
    assert DEFAULT_ACTIVE_DEADLINE_SECONDS == 4 * 60 * 60
    manifest = _baseline_manifest()
    assert manifest["spec"]["activeDeadlineSeconds"] == 4 * 60 * 60


def test_script_uses_single_azcopy_s3blob() -> None:
    """The pod script must transfer each shard with a SINGLE
    `azcopy copy --from-to=S3Blob` over an `--include-pattern`, not the old
    per-file `curl | azcopy --from-to=PipeBlob` serial loop (which threw
    away azcopy's native multi-file parallelism and could never converge
    for `nt`/`core_nt` within the Job deadline)."""
    assert "--from-to=S3Blob" in PREPARE_DB_AKS_SCRIPT
    assert "--include-pattern" in PREPARE_DB_AKS_SCRIPT
    # The old per-file PipeBlob loop and its machinery must be gone.
    assert "PipeBlob" not in PREPARE_DB_AKS_SCRIPT
    assert "blob_content_length" not in PREPARE_DB_AKS_SCRIPT
    assert "while IFS= read" not in PREPARE_DB_AKS_SCRIPT
    assert "mktemp" not in PREPARE_DB_AKS_SCRIPT
    # `set -euo pipefail` is what makes a setup failure fail the shard.
    assert "set -euo pipefail" in PREPARE_DB_AKS_SCRIPT


def test_script_overwrites_to_heal_partial_blobs() -> None:
    """The glob copy uses `--overwrite=true` so a re-run re-fetches files.
    The surgical repair then deletes + re-copies any blob azcopy could not
    commit (corrupt uncommitted block list)."""
    assert "--overwrite=true" in PREPARE_DB_AKS_SCRIPT


def test_script_repairs_only_failed_files_not_full_redownload() -> None:
    """A failed transfer (azcopy CompletedWithErrors / exit 1) must NOT
    trigger a full-shard re-download.

    Root cause verified live 2026-06-05: some destination blobs carry a
    corrupt UNCOMMITTED block list from an earlier interrupted upload, so
    azcopy's Put-Block-From-URL fails them with `400 InvalidBlobOrBlock` --
    a PERMANENT per-blob error `--overwrite=true` cannot clear. Re-running
    the glob re-downloads the ~200 GB shard (azcopy does NOT skip the
    committed blobs for S3->Blob) and hits the same poisoned blob again =>
    non-convergence. The fix extracts ONLY the failed files, DELETES each bad
    dest blob to clear its block list, then re-copies that single file."""
    # Failed-file extraction from the glob job.
    assert "--with-status=Failed" in PREPARE_DB_AKS_SCRIPT
    assert "azcopy jobs show" in PREPARE_DB_AKS_SCRIPT
    # Delete the poisoned dest blob before re-copying it.
    assert "azcopy remove" in PREPARE_DB_AKS_SCRIPT
    # Bounded repair rounds on the shrinking failed set.
    assert "ELB_AZCOPY_MAX_ATTEMPTS" in PREPARE_DB_AKS_SCRIPT
    assert 'while [ "${#PAIRS[@]}" -gt 0 ]' in PREPARE_DB_AKS_SCRIPT
    # The discredited ifSourceNewer "resume" must be gone (it reported
    # Skipped=0 for S3->Blob and re-downloaded everything).
    assert "ifSourceNewer" not in PREPARE_DB_AKS_SCRIPT


def test_script_builds_include_pattern_from_basenames() -> None:
    """The include-pattern is built from the shard file's basenames via
    python3 (the image ships no GNU text tools, and `head` in a pipefail
    pipeline would SIGPIPE the shard). It must NOT use `--list-of-files`,
    which nests `<snapshot>/` under the destination and breaks the flat
    `blast-db/<db>/<file>` layout elastic-blast requires."""
    assert "PATTERN=$(python3 -c" in PREPARE_DB_AKS_SCRIPT
    assert 'split("/")[-1]' in PREPARE_DB_AKS_SCRIPT
    assert "--list-of-files" not in PREPARE_DB_AKS_SCRIPT


def test_script_flat_destination_layout() -> None:
    """Trailing `/*` source + `blast-db/<db>/` destination yields the flat
    layout `blast-db/<db>/<file>` that elastic-blast resolves."""
    assert 'SRC="${NCBI_BASE}/${SOURCE_VERSION}/*"' in PREPARE_DB_AKS_SCRIPT
    assert (
        'DEST_BASE="https://${STORAGE_ACCOUNT}.${BLOB_SUFFIX}/blast-db/${DB_NAME}"'
        in PREPARE_DB_AKS_SCRIPT
    )


def test_script_uses_no_awk() -> None:
    """The prepare-db script must not shell out to awk.

    The job runs in `mcr.microsoft.com/azure-cli` (Azure Linux / Mariner),
    which ships no awk, so any awk call aborts the pod under
    `set -euo pipefail`. The image guarantees python3 (azure-cli is a python
    app), so all text munging must stay python3-based."""
    assert "awk" not in PREPARE_DB_AKS_SCRIPT


def test_script_pins_azcopy_1028() -> None:
    """azcopy must be pinned to 10.28.0 from GitHub releases, NOT the
    `aka.ms` redirect.

    Regression guard for the version crash found live 2026-06-05: aka.ms now
    serves azcopy 10.32.4, which panics (nil deref in getSourceServiceClient)
    on EVERY `--from-to=S3Blob` copy. 10.28.0 handles S3Blob correctly. The
    pin is overridable via `ELB_AZCOPY_URL` once a newer build is verified to
    fix the S3Blob crash. The single binary is still extracted via stdlib
    tarfile because the image ships no GNU tar."""
    assert "azcopy_linux_amd64_10.28.0.tar.gz" in PREPARE_DB_AKS_SCRIPT
    assert "ELB_AZCOPY_URL" in PREPARE_DB_AKS_SCRIPT
    assert "aka.ms/downloadazcopy-v10-linux" not in PREPARE_DB_AKS_SCRIPT
    assert "import tarfile" in PREPARE_DB_AKS_SCRIPT
    assert "/usr/local/bin/azcopy" in PREPARE_DB_AKS_SCRIPT


def test_labels_are_k8s_safe() -> None:
    manifest = _baseline_manifest(db_name="bad_chars.are_ok")
    labels = manifest["metadata"]["labels"]
    pod_labels = manifest["spec"]["template"]["metadata"]["labels"]
    import re

    label_re = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")
    for v in labels.values():
        assert label_re.match(v), v
    for v in pod_labels.values():
        assert label_re.match(v), v

