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


def test_manifest_volumes_include_scripts_and_tmp() -> None:
    manifest = _baseline_manifest()
    volumes = manifest["spec"]["template"]["spec"]["volumes"]
    by_name = {v["name"]: v for v in volumes}
    assert "scripts" in by_name
    assert by_name["scripts"]["configMap"]["name"] == "prepare-db-corent-202605210105"
    assert by_name["scripts"]["configMap"]["defaultMode"] == 0o755
    assert "tmp" in by_name
    assert by_name["tmp"]["emptyDir"]["medium"] == "Memory"


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
