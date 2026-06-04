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
    assert spec["backoffLimit"] >= 0
    assert spec["backoffLimit"] <= 5  # don't burn budget retrying forever


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


def test_manifest_default_active_deadline_is_45_minutes() -> None:
    """Phase 1.5: bumped from 30 -> 45 min so the slowest shard in a
    5-node throttled run still has headroom for ~10-15 large `.nsq`
    files after its peers finish."""
    assert DEFAULT_ACTIVE_DEADLINE_SECONDS == 2700
    manifest = _baseline_manifest()
    assert manifest["spec"]["activeDeadlineSeconds"] == 2700


def test_script_streams_via_pipeblob() -> None:
    """The pod script must stream NCBI -> Azure Blob via
    `azcopy --from-to=PipeBlob`. Anything else (server-side
    `--from-to=BlobBlob`, on-disk staging via `mktemp`) reintroduces
    the OOM or per-node-NAT regressions."""
    assert "--from-to=PipeBlob" in PREPARE_DB_AKS_SCRIPT
    # The old on-disk path used `mktemp /tmp/prepare-db-XXXXXX` — must be
    # gone now or the `/tmp` emptyDir would still be required.
    assert "mktemp" not in PREPARE_DB_AKS_SCRIPT
    # `set -euo pipefail` is what makes a curl-side failure fail the
    # shard cleanly.
    assert "set -euo pipefail" in PREPARE_DB_AKS_SCRIPT


def test_pipeblob_destination_is_single_positional() -> None:
    """PipeBlob copy must pass the destination as the only positional
    argument (`azcopy copy "<dst>" --from-to=PipeBlob`).

    Regression guard for the 0-byte-upload outage: azcopy >= 10.32 rejects
    the two-positional form `azcopy copy --from-to=PipeBlob "" "<dst>"` —
    it treats the empty first positional as the source and aborts the copy
    immediately with a non-zero exit and no transfer, so every shard
    "ran" but uploaded nothing. Pin the destination-first form and forbid
    the empty `""` placeholder so the broken syntax can never come back."""
    assert 'azcopy copy "$dst_url" --from-to=PipeBlob' in PREPARE_DB_AKS_SCRIPT
    assert '--from-to=PipeBlob "" "$dst_url"' not in PREPARE_DB_AKS_SCRIPT


def test_script_skips_already_uploaded_blobs() -> None:
    """Per-file idempotency: `azcopy list` against the destination URL,
    skip if it already has a ContentLength. This makes a backoffLimit
    retry replay only the failed files instead of refetching all 750+."""
    assert "azcopy list" in PREPARE_DB_AKS_SCRIPT
    assert "ContentLength" in PREPARE_DB_AKS_SCRIPT
    # `skip` counter is what the DONE log line surfaces, so the user can
    # tell "0 skipped on first run vs N skipped on retry" at a glance.
    assert "skip=$((skip + 1))" in PREPARE_DB_AKS_SCRIPT
    assert "skip=${skip}" in PREPARE_DB_AKS_SCRIPT


def test_idempotency_skip_requires_nonzero_content_length() -> None:
    """Idempotency must skip ONLY fully-uploaded blobs, never 0-byte
    placeholders.

    Regression guard for the corrupt-DB outage: an aborted server-side
    copy (legacy prepare path) leaves a 0-byte blob whose ContentLength
    KEY exists but whose value is 0. The old `grep -q '"ContentLength"'`
    check skipped those too, so the AKS rerun left ~1000 truncated blobs
    and a corrupt BLAST database. The skip decision must therefore gate on
    ContentLength > 0, with missing / 0-byte / parse-fail all falling
    through to a clean re-download."""
    # The brittle key-presence grep must be gone.
    assert "grep -q '\"ContentLength\"'" not in PREPARE_DB_AKS_SCRIPT
    # Skip is gated on a strictly-positive size via the shared helper.
    assert "blob_content_length" in PREPARE_DB_AKS_SCRIPT
    assert 'existing_len=$(blob_content_length "$dst_url")' in PREPARE_DB_AKS_SCRIPT
    assert '[ "$existing_len" -gt 0 ]' in PREPARE_DB_AKS_SCRIPT
    # PARSE_FAIL (azcopy schema drift) must NOT count as "already uploaded".
    assert '"$existing_len" != "PARSE_FAIL"' in PREPARE_DB_AKS_SCRIPT


def test_script_uses_no_awk() -> None:
    """The prepare-db script must not shell out to awk.

    Regression guard for the shard-wide failure found 2026-06-04: the
    Content-Length pre-flight parsed `curl -sIL` output with
    `awk 'BEGIN{IGNORECASE=1} /^content-length:/ {...}'`. The job runs in
    `mcr.microsoft.com/azure-cli` (Azure Linux / Mariner), which ships NO
    awk, so the first sampled file (every VERIFY_EVERY_N-th) hit
    `awk: command not found` and `set -euo pipefail` killed the pod. With
    the verify sampling at 1/10 this failed every shard on its 10th file,
    backoffLimit was exhausted, and the Job reported `Failed 0/10` while
    ~77% of nt was already staged. The image guarantees python3 (azure-cli
    is a python app and the script already uses python3), so the parser
    must stay awk-free."""
    assert "awk" not in PREPARE_DB_AKS_SCRIPT


def test_content_length_preflight_uses_python3() -> None:
    """The NCBI Content-Length pre-flight must parse with python3.

    Pins the awk-free replacement: `curl -sIL` follows redirects and emits
    a `content-length` header for every hop, so the parser keeps the LAST
    value seen (the final 200 response's real size) rather than the first
    (a 301/302 hop carries the wrong size or none). This is the value the
    post-upload verify compares against to catch NCBI truncations."""
    assert "content-length:" in PREPARE_DB_AKS_SCRIPT.lower()
    # The pre-flight feeds curl's header dump into a python3 parser.
    assert 'curl -sIL --retry 3 --retry-delay 10 --max-time 60 \\' in (
        PREPARE_DB_AKS_SCRIPT
    )
    assert "python3 -c" in PREPARE_DB_AKS_SCRIPT


def test_script_bootstraps_azcopy_from_aka_ms() -> None:
    """The pinned azure-cli image does not bundle azcopy or GNU tar, so
    the script must download azcopy from aka.ms and extract it via
    Python's stdlib tarfile module. Both pieces are load-bearing."""
    assert "aka.ms/downloadazcopy-v10-linux" in PREPARE_DB_AKS_SCRIPT
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
