"""Tests for Terminal Patch Elastic BLAST behavior.

Responsibility: Tests for Terminal Patch Elastic BLAST behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_load_patch_module`,
`test_patch_kubectl_transient_retries_honours_total_deadline`,
`test_patch_kubectl_transient_retries_preserves_non_kubectl_path`,
`test_patch_init_shard_script_writes_hardened_cache_skip`,
`test_patch_init_shard_script_is_idempotent`,
`test_patch_init_shard_script_updates_installed_package_copy`,
`test_patch_azure_traits_adds_dashboard_v7_skus`,
`test_patch_azure_cli_glue_clears_cleanup_stack_for_json_submit_success`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_terminal_patch_elastic_blast.py`.
"""

# ruff: noqa: E501 -- upstream source fixtures intentionally preserve long lines

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _load_patch_module():
    module_path = Path(__file__).resolve().parents[2] / "terminal" / "patch_elastic_blast.py"
    spec = importlib.util.spec_from_file_location("terminal_patch_elastic_blast", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_SAFE_EXEC_STUB = (
    """import os
import re
import subprocess
import datetime
from typing import Optional

class SafeExecError(Exception):
    def __init__(self, returncode, message):
        self.returncode = returncode
        self.message = message

    def __str__(self):
        return self.message

"""
    "def safe_exec(cmd: list[str] | str, env: dict[str, str] | None = None, "
    "timeout: Optional[float] = 60) -> subprocess.CompletedProcess:\n"
    """
    p = subprocess.CompletedProcess(cmd, 0)
    return p

def safe_exec_print(cmd):
    return cmd
"""
)


def _patched_util_module(tmp_path: Path):
    patch_module = _load_patch_module()
    target = tmp_path / "src" / "elastic_blast" / "util.py"
    target.parent.mkdir(parents=True)
    target.write_text(_SAFE_EXEC_STUB)
    patch_module.patch_kubectl_transient_retries(tmp_path)
    spec = importlib.util.spec_from_file_location("patched_elb_util", target)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_patch_kubectl_transient_retries_then_succeeds(tmp_path: Path, monkeypatch) -> None:
    module = _patched_util_module(tmp_path)
    calls: list[object] = []
    sleeps: list[int] = []

    def _run(cmd, **_kwargs):
        calls.append(cmd)
        if len(calls) < 3:
            raise module.SafeExecError(503, "Error from server (ServiceUnavailable): 503")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(module, "_safe_exec_once", _run)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    result = module.safe_exec(["kubectl", "--context=incluster", "get", "jobs"])

    assert result.returncode == 0
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_patch_kubectl_transient_retries_preserves_non_kubectl_path(
    tmp_path: Path, monkeypatch
) -> None:
    module = _patched_util_module(tmp_path)
    calls: list[tuple[object, object]] = []

    def _run(cmd, *, timeout, **_kwargs):
        calls.append((cmd, timeout))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setenv("ELB_KUBECTL_TRANSIENT_DEADLINE_SECONDS", "invalid")
    monkeypatch.setattr(module, "_safe_exec_once", _run)
    monkeypatch.setattr(
        module.time,
        "monotonic",
        lambda: (_ for _ in ()).throw(AssertionError("deadline path entered")),
    )

    result = module.safe_exec(["azcopy", "list", "https://example.invalid"], timeout=None)

    assert result.returncode == 0
    assert calls == [(["azcopy", "list", "https://example.invalid"], None)]


def test_patch_kubectl_transient_retries_never_replays_unsafe_command(
    tmp_path: Path, monkeypatch
) -> None:
    module = _patched_util_module(tmp_path)
    calls: list[object] = []

    def _run(cmd, **_kwargs):
        calls.append(cmd)
        raise module.SafeExecError(503, "Error from server (ServiceUnavailable): 503")

    monkeypatch.setattr(module, "_safe_exec_once", _run)

    import pytest

    with pytest.raises(module.SafeExecError):
        module.safe_exec(["kubectl", "create", "secret", "generic", "value"])
    assert len(calls) == 1


def test_patch_kubectl_transient_retries_only_replay_safe_mutations(
    tmp_path: Path, monkeypatch
) -> None:
    module = _patched_util_module(tmp_path)
    calls: list[object] = []

    def _run(cmd, **_kwargs):
        calls.append(cmd)
        raise module.SafeExecError(503, "Error from server (ServiceUnavailable): 503")

    monkeypatch.setattr(module, "_safe_exec_once", _run)

    import pytest

    with pytest.raises(module.SafeExecError):
        module.safe_exec(["kubectl", "delete", "job", "already-gone"])
    with pytest.raises(module.SafeExecError):
        module.safe_exec(["kubectl", "label", "node", "n1", "ordinal=0"])
    assert len(calls) == 2


def test_patch_kubectl_transient_retries_reject_auth_failures(tmp_path: Path, monkeypatch) -> None:
    module = _patched_util_module(tmp_path)
    calls: list[object] = []

    def _run(cmd, **_kwargs):
        calls.append(cmd)
        raise module.SafeExecError(403, "Error from server (Forbidden): 503 permission denied")

    monkeypatch.setattr(module, "_safe_exec_once", _run)

    import pytest

    with pytest.raises(module.SafeExecError):
        module.safe_exec(["kubectl", "get", "jobs"])
    assert len(calls) == 1


def test_patch_kubectl_transient_retries_exhausts_six_attempt_budget(
    tmp_path: Path, monkeypatch
) -> None:
    module = _patched_util_module(tmp_path)
    calls: list[object] = []
    sleeps: list[int] = []

    def _run(cmd, **_kwargs):
        calls.append(cmd)
        raise module.SafeExecError(503, "Error from server (ServiceUnavailable): 503")

    monkeypatch.setattr(module, "_safe_exec_once", _run)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    import pytest

    with pytest.raises(module.SafeExecError):
        module.safe_exec(["kubectl", "get", "jobs"])
    assert len(calls) == 6
    assert sleeps == [1, 2, 4, 4, 4]


def test_patch_kubectl_transient_retries_honours_total_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    module = _patched_util_module(tmp_path)
    calls: list[float] = []
    sleeps: list[int] = []
    now = [0.0]

    def _run(_cmd, *, timeout, **_kwargs):
        calls.append(timeout)
        now[0] += 2.5
        raise module.SafeExecError(503, "Error from server (ServiceUnavailable): 503")

    def _sleep(delay: int) -> None:
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setenv("ELB_KUBECTL_TRANSIENT_DEADLINE_SECONDS", "5")
    monkeypatch.setattr(module, "_safe_exec_once", _run)
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module.time, "sleep", _sleep)

    import pytest

    with pytest.raises(module.SafeExecError):
        module.safe_exec(["kubectl", "get", "jobs"], timeout=60)
    assert calls == [5.0, 1.5]
    assert sleeps == [1]


def test_patch_init_shard_script_writes_hardened_cache_skip(tmp_path: Path) -> None:
    patch_module = _load_patch_module()
    script_path = tmp_path / "src" / "elastic_blast" / "templates" / "scripts"
    script_path.mkdir(parents=True)
    target = script_path / "init-db-shard-aks.sh"
    target.write_text("#!/bin/bash\ntouch .download-complete\n")

    patch_module.patch_init_shard_script(tmp_path)

    text = target.read_text()
    skip_prefix = text.split('echo "DOWNLOAD_SKIP existing shard=${ELB_SHARD_IDX}"', 1)[0]
    assert 'cd "${ELB_BLASTDB_DIR:-/blast/blastdb}"' in text
    assert "CLEANUP partial downloads" in text
    assert "find . -maxdepth 1 -name '.azDownload-*' -exec rm -rf {} +" in text
    assert '[ -f "$CACHE_COMPLETE" ]' in text
    assert "TAXDB_SKIP taxdb files not present in DB prefix" in text
    assert "CACHE_INCOMPLETE missing ${volume}.${payload_ext}" in text
    assert "CACHE_STALE source-version mismatch" in text
    assert "CACHE_STALE shard manifest mismatch" in text
    assert "CACHE_STALE shard layout mismatch" in text
    assert "CACHE_STALE shard alias mismatch" in text
    assert 'cp /tmp/shard.nal "./${ELB_DB}.nal.tmp"' in text
    assert "Resolving DB metadata: ${METADATA_URL}" in text
    assert "${DB_BASE_URL}${ORIG_DB}-metadata.json" in text
    assert "DB metadata lookup failed after retries; refusing unversioned shard staging" in text
    assert "DB source version changed after Job creation" in text
    assert "CACHE_UNVERIFIED expected source version is unavailable" in text
    assert "write_volpaths" in text
    assert 'CACHE_COMPLETE=".elb-cache.${ELB_DB}.complete"' in text
    assert "printf '%s' ok > \"${CACHE_COMPLETE}.tmp\"" in text
    # The blastdbcmd integrity probe gates the skip so a vol/lmdb-mismatch cache
    # is re-downloaded instead of skipped onto a broken DB.
    assert "CACHE_CORRUPT blastdbcmd integrity probe failed" in skip_prefix
    assert 'blastdbcmd -db "$ELB_DB" -info' in skip_prefix
    assert 'printf \'%s\' "$EXPECTED_SOURCE_VERSION" > "${CACHE_SOURCE_VERSION}.tmp"' in text
    assert "if [ -s .download-complete ]" not in text
    assert "touch .download-complete" not in text
    assert "taxonomy4blast.sqlite3" not in skip_prefix
    # Regression guard: the `-taxids`/`-negative_taxids` taxonomy FILTER memory-maps the
    # DB-prefix seqid->taxid index `${ORIG_DB}.nos` and `${ORIG_DB}.not`. Omitting them
    # made sharded core_nt runs with a taxon include/exclude abort with blastn exit 255
    # ("the file must exist: '<db>.not'"). They must be part of the download pattern.
    download_pattern = text.split('echo "Downloading with pattern: ${PATTERN}"', 1)[0]
    assert "${ORIG_DB}.nos" in download_pattern
    assert "${ORIG_DB}.not" in download_pattern
    # Self-heal guard: a cache staged before the .nos/.not fix (taxonomy OUTPUT
    # files present, FILTER index absent) must invalidate .download-complete so
    # the corrected pattern re-stages them on the next warmup.
    assert "CACHE_INCOMPLETE missing taxonomy filter index" in text
    assert '[ -s "${ORIG_DB}.ntf" ]' in text
    assert "downloaded taxonomy filter index is incomplete" in text
    assert "downloaded DB failed blastdbcmd integrity probe" in text
    cleanup = text.split("# Shared taxonomy files", 1)[1].split(
        'if [ "$LAYOUT_AVAILABLE" = "1" ]; then', 1
    )[0]
    assert "${ORIG_DB}.not" in cleanup
    assert "taxdb.btd" not in cleanup
    assert "taxonomy4blast.sqlite3" not in cleanup
    assert "--overwrite=true" in text
    assert text.index("CACHE_UNVERIFIED expected source version is unavailable") < text.index(
        'echo "DOWNLOAD_SKIP existing shard=${ELB_SHARD_IDX}"'
    )
    assert text.index("downloaded DB failed blastdbcmd integrity probe") < text.index(
        'mv "${CACHE_COMPLETE}.tmp" "$CACHE_COMPLETE"'
    )
    assert text.index('mv "${CACHE_SOURCE_VERSION}.tmp" "$CACHE_SOURCE_VERSION"') < (
        text.index('mv "${CACHE_COMPLETE}.tmp" "$CACHE_COMPLETE"')
    )
    assert 'STAGE_LOCK_WAIT_SECONDS="${ELB_STAGE_LOCK_TIMEOUT_SECONDS:-2400}"' in text
    assert 'flock -w "$STAGE_LOCK_WAIT_SECONDS" 9' in text
    assert '"$STAGE_LOCK_WAIT_SECONDS" -gt 5400' in text
    assert "waited_seconds=" in text
    assert text.index("STAGE_LOCK_ACQUIRED") < text.index("CLEANUP partial downloads")
    assert 'STAGE_LOCK_FILE=".elb-stage.lock"' in text
    assert "export ELB_STAGE_LOCK_HELD=1" in text
    assert '[[ "$ELB_DB" =~ ^(.+)_shard_([0-9]+)$ ]]' in text
    assert "${ELB_DB%%_shard_*}" not in text
    assert "exit 75" in text
    assert 'LAYOUT_URL="${SHARD_URL}${ELB_DB}.layout"' in text
    assert "schema ${SHARD_LAYOUT_SCHEMA} requires shard layout metadata" in text
    assert "LAYOUT_VERIFIED sha256=" in text
    assert "shard layout digest mismatch" in text
    assert 'for volume in "${VOLUMES[@]}"; do' in text
    syntax = subprocess.run(
        ["/bin/bash", "-n"],
        input=text,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert syntax.returncode == 0, syntax.stderr


def test_verify_runtime_identity_templates_requires_job_and_pod_labels(
    tmp_path: Path,
) -> None:
    patch_module = _load_patch_module()
    templates = tmp_path / "src" / "elastic_blast" / "templates"
    templates.mkdir(parents=True)
    names = (
        "job-init-ssd-shard-aks.yaml.template",
        "blast-batch-job-shard-ssd-aks.yaml.template",
        "elb-finalizer-aks.yaml.template",
    )
    marker = 'elb-job-id: "${BLAST_ELB_JOB_ID}"\n'
    for name in names:
        (templates / name).write_text(marker * 2)

    patch_module.verify_runtime_identity_templates(tmp_path)

    (templates / names[0]).write_text(marker)

    import pytest

    with pytest.raises(RuntimeError, match="runtime identity label on Job and Pod"):
        patch_module.verify_runtime_identity_templates(tmp_path)


_INIT_JOB_WAIT_LOOPS = """from __future__ import annotations

import json
import logging
import time
from timeit import default_timer as timer


class ElasticBlastConfig:
    pass


class SafeExecError(Exception):
    pass


def initialize_local_ssd(cfg: ElasticBlastConfig, query_files=[], wait=None):
        # wait for multiple jobs
        timeout = init_blastdb_minutes_timeout * 60
        sec2wait = 20
        while timeout > 0:
            cmd = f'kubectl --context={cfg.appstate.k8s_ctx} get jobs -l elb-job-id={cfg.azure.elb_job_id} -o jsonpath=' \\
                '{.items[?(@.status.active)].metadata.name}{\'\\t\'}' \\
                '{.items[?(@.status.failed)].metadata.name}{\'\\t\'}' \\
                '{.items[?(@.status.succeeded)].metadata.name}'
            if dry_run:
                logging.info(cmd)
                res = '\\t\\t' + \\
                    ' '.join([f'init-ssd-{n}' for n in range(num_nodes)])
            else:
                proc = safe_exec(cmd)
                res = handle_error(proc.stdout)
                logging.debug(res)
            active, failed, succeeded = res.split('\\t')
            if failed:
                proc = safe_exec(f'kubectl --context={cfg.appstate.k8s_ctx} logs -l app=setup')
                for line in handle_error(proc.stdout).split('\\n'):
                    logging.debug(line)
                raise RuntimeError(f'Local SSD initialization jobs failed: {failed}')
            if not active:
                logging.debug(f'Local SSD initialization jobs succeeded: {succeeded}')
                break
            time.sleep(sec2wait)
            timeout -= sec2wait
        if timeout < 0:
            raise TimeoutError(f'{d} jobs timed out')


def initialize_local_ssd_sharded(cfg: ElasticBlastConfig, query_files=[], wait=None):
        # Wait for all init jobs to complete
        timeout = init_blastdb_minutes_timeout * 60
        sec2wait = 20
        while timeout > 0:
            cmd = f'kubectl --context={cfg.appstate.k8s_ctx} get jobs -l app=setup,elb-job-id={cfg.azure.elb_job_id} -o jsonpath=' \\
                '{.items[?(@.status.active)].metadata.name}{\'\\t\'}' \\
                '{.items[?(@.status.failed)].metadata.name}{\'\\t\'}' \\
                '{.items[?(@.status.succeeded)].metadata.name}'
            if dry_run:
                logging.info(cmd)
                res = '\\t\\t' + ' '.join([f'init-ssd-{n}' for n in range(num_shards)])
            else:
                proc = safe_exec(cmd)
                res = handle_error(proc.stdout)
                logging.debug(res)
            active, failed, succeeded = res.split('\\t')
            if failed:
                raise RuntimeError(f'Shard init jobs failed: {failed}')
            if not active:
                logging.debug(f'Shard init jobs succeeded: {succeeded}')
                break
            time.sleep(sec2wait)
            timeout -= sec2wait
        if timeout < 0:
            raise TimeoutError('Shard init jobs timed out')
"""


def test_patch_init_job_retry_tolerance_waits_for_terminal_condition(
    tmp_path: Path,
) -> None:
    patch_module = _load_patch_module()
    source_dir = tmp_path / "src" / "elastic_blast"
    source_dir.mkdir(parents=True)
    target = source_dir / "kubernetes.py"
    target.write_text(_INIT_JOB_WAIT_LOOPS)

    patch_module.patch_init_job_retry_tolerance(tmp_path)

    text = target.read_text()
    assert text.count("def _elb_init_job_states(") == 1
    assert text.count("def _wait_for_elb_init_jobs(") == 1
    assert text.count("_wait_for_elb_init_jobs(") == 3
    assert "condition.get('type') == 'Failed'" in text
    assert "failed-Pod count while Kubernetes still has backoff retries" in text
    assert "expected_job_names.issubset(succeeded)" in text
    assert "timeout=max(1, min(20, remaining))" in text
    assert "timeout_seconds=init_blastdb_minutes_timeout * 60" in text
    assert "for n in range(min(num_shards, num_nodes))" in text
    assert text.count("f'init-ssd-{cfg.azure.elb_job_id}-{n}'") == 2
    assert "-l {selector} -o json" in text
    assert "--all-containers=true --prefix=true --tail=-1" in text
    assert "Shard init jobs failed: {failed}" not in text
    assert "Local SSD initialization jobs failed: {failed}" not in text


def test_patch_init_job_retry_tolerance_is_idempotent(tmp_path: Path) -> None:
    patch_module = _load_patch_module()
    source_dir = tmp_path / "src" / "elastic_blast"
    source_dir.mkdir(parents=True)
    target = source_dir / "kubernetes.py"
    target.write_text(_INIT_JOB_WAIT_LOOPS)

    patch_module.patch_init_job_retry_tolerance(tmp_path)
    once = target.read_text()
    patch_module.patch_init_job_retry_tolerance(tmp_path)

    assert target.read_text() == once


def _patched_init_wait_module(tmp_path: Path):
    patch_module = _load_patch_module()
    source_dir = tmp_path / "src" / "elastic_blast"
    source_dir.mkdir(parents=True)
    target = source_dir / "kubernetes.py"
    target.write_text(_INIT_JOB_WAIT_LOOPS)
    patch_module.patch_init_job_retry_tolerance(tmp_path)
    spec = importlib.util.spec_from_file_location("patched_init_wait", target)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_init_job_state_classifier_distinguishes_pod_retry_from_job_failure(
    tmp_path: Path,
) -> None:
    module = _patched_init_wait_module(tmp_path)
    payload = {
        "items": [
            {
                "metadata": {"name": "retrying"},
                "status": {"failed": 1, "conditions": []},
            },
            {
                "metadata": {"name": "terminal-failure"},
                "status": {
                    "failed": 4,
                    "conditions": [{"type": "Failed", "status": "True"}],
                },
            },
            {
                "metadata": {"name": "complete"},
                "status": {
                    "succeeded": 1,
                    "conditions": [{"type": "Complete", "status": "True"}],
                },
            },
        ]
    }

    pending, failed, succeeded = module._elb_init_job_states(json.dumps(payload))

    assert pending == {"retrying"}
    assert failed == {"terminal-failure"}
    assert succeeded == {"complete"}


def test_init_job_wait_does_not_treat_missing_jobs_as_success(tmp_path: Path, monkeypatch) -> None:
    module = _patched_init_wait_module(tmp_path)
    clock = [0.0]

    class Config:
        class AppState:
            k8s_ctx = "ctx"

        appstate = AppState()

    def fake_exec(_cmd, *, timeout):
        clock[0] += timeout
        return subprocess.CompletedProcess(_cmd, 0, stdout='{"items": []}', stderr="")

    monkeypatch.setattr(module, "safe_exec", fake_exec, raising=False)
    monkeypatch.setattr(module, "handle_error", lambda value: value, raising=False)
    monkeypatch.setattr(module, "timer", lambda: clock[0])
    monkeypatch.setattr(
        module.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    with pytest.raises(TimeoutError, match="timed out: init-ssd-job-abc-0"):
        module._wait_for_elb_init_jobs(
            Config(),
            selector="elb-job-id=job-abc",
            expected_job_names={"init-ssd-job-abc-0"},
            timeout_seconds=2,
            failure_prefix="Shard init jobs failed",
            dry_run=False,
        )


def test_init_job_log_collection_error_preserves_terminal_failure(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    module = _patched_init_wait_module(tmp_path)

    class Config:
        class AppState:
            k8s_ctx = "ctx"

        appstate = AppState()

    terminal_failure = json.dumps(
        {
            "items": [
                {
                    "metadata": {"name": "init-ssd-job-abc-0"},
                    "status": {
                        "conditions": [{"type": "Failed", "status": "True"}],
                    },
                }
            ]
        }
    )

    def fake_exec(command: str, *, timeout: float):
        del timeout
        stdout = "broken-log-payload" if " logs " in command else terminal_failure
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    def fake_handle_error(value: str) -> str:
        if value == "broken-log-payload":
            raise ValueError("log parser failed")
        return value

    monkeypatch.setattr(module, "safe_exec", fake_exec, raising=False)
    monkeypatch.setattr(module, "handle_error", fake_handle_error, raising=False)

    with pytest.raises(
        RuntimeError,
        match="Shard init jobs failed: init-ssd-job-abc-0",
    ):
        module._wait_for_elb_init_jobs(
            Config(),
            selector="elb-job-id=job-abc",
            expected_job_names={"init-ssd-job-abc-0"},
            timeout_seconds=30,
            failure_prefix="Shard init jobs failed",
            dry_run=False,
        )

    assert "Failed to collect init Job logs: log parser failed" in caplog.text


def test_patch_unique_init_job_names_use_full_canonical_runtime_id(
    tmp_path: Path,
) -> None:
    patch_module = _load_patch_module()
    templates = tmp_path / "src" / "elastic_blast" / "templates"
    templates.mkdir(parents=True)
    names = (
        "job-init-local-ssd-aks.yaml.template",
        "job-init-ssd-shard-aks.yaml.template",
    )
    for name in names:
        (templates / name).write_text("  name: init-ssd-${NODE_ORDINAL}\n")

    patch_module.patch_unique_init_ssd_job_names(tmp_path)
    snapshots = {name: (templates / name).read_text() for name in names}
    patch_module.patch_unique_init_ssd_job_names(tmp_path)

    for name in names:
        text = (templates / name).read_text()
        assert text == snapshots[name]
        assert "name: init-ssd-${BLAST_ELB_JOB_ID}-${NODE_ORDINAL}" in text
        assert "BLAST_ELB_JOB_ID_SHORT" not in text


_ELB_CONFIG_OUTFMT_GATE = (
    "            if (\n"
    "                outfmt_code not in {'5', '6'}\n"
    "                or (outfmt_code == '5' and outfmt_extended)\n"
    "                or (outfmt_code == '6' and outfmt_extended and not "
    "outfmt_extended.startswith('std'))\n"
    "            ):\n"
    "                errors.append(\n"
    "                    'Partitioned BLAST requires outfmt 5 without extended fields, '\n"
    "                    'outfmt 6, or \"6 std...\"; '\n"
    "                    f'{outfmt} is not supported for merge')\n"
)


def _write_elb_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "src" / "elastic_blast"
    config_dir.mkdir(parents=True)
    target = config_dir / "elb_config.py"
    target.write_text("# elb_config stub\n" + _ELB_CONFIG_OUTFMT_GATE)
    return target


def test_patch_partitioned_outfmt_gate_allows_outfmt7(tmp_path: Path) -> None:
    patch_module = _load_patch_module()
    target = _write_elb_config(tmp_path)

    patch_module.patch_partitioned_outfmt_gate(tmp_path)

    text = target.read_text()
    # The gate now allows any tabular 6/7 layout (incl. non-std extended);
    # the per-code `startswith('std')` restriction is removed.
    assert "outfmt_code not in {'5', '6', '7'}" in text
    assert "outfmt_extended.startswith('std')" not in text
    assert "outfmt_code == '7' and outfmt_extended" not in text
    assert "tabular outfmt 6/7 (optionally with an extended field list)" in text


def test_patch_partitioned_outfmt_gate_is_idempotent(tmp_path: Path) -> None:
    patch_module = _load_patch_module()
    target = _write_elb_config(tmp_path)

    patch_module.patch_partitioned_outfmt_gate(tmp_path)
    once = target.read_text()
    patch_module.patch_partitioned_outfmt_gate(tmp_path)

    assert target.read_text() == once
    # The widened gate is present exactly once (no double application).
    assert once.count("outfmt_code not in {'5', '6', '7'}") == 1


_BLAST_RUN_AKS_STUB = """#!/bin/bash
set -uo pipefail
# shellcheck disable=SC2086
TIME="$DATE_NOW run start $JOB_NUM $ELB_BLAST_PROGRAM $ELB_DB %e %U %S %P" \\
\\time -o "$BLAST_RUNTIME" \\
$ELB_BLAST_PROGRAM \\
-db "$ELB_DB" \\
-query "$QUERY_DIR/batch_${JOB_NUM}.fa" \\
-out "$RESULTS_DIR/batch_${JOB_NUM}-${ELB_BLAST_PROGRAM}-${ELB_DB_SAFE}.out" \\
-num_threads "$ELB_NUM_CPUS" \\
$ELB_BLAST_OPTIONS \\
2>"$ERROR_FILE"
BLAST_EXIT_CODE=$?
"""

# A probe inserted right before the patched TIME= invocation (after the argv
# rebuild) that prints each argv element on its own line for exact assertions.
_ARGV_PROBE = 'for _a in "${ELB_BLAST_ARGV[@]}"; do printf "ARG[%s]\\n" "$_a"; done\nexit 0\n'


def _run_argv_rebuild(tmp_path: Path, blast_options: str) -> list[str]:
    patch_module = _load_patch_module()
    script = tmp_path / "blast-run-aks.sh"
    script.write_text(_BLAST_RUN_AKS_STUB)
    patch_module.patch_blast_run_aks_outfmt_argv(script)
    text = script.read_text()
    anchor = '# shellcheck disable=SC2086\nTIME="$DATE_NOW run start'
    assert anchor in text
    script.write_text(text.replace(anchor, _ARGV_PROBE + anchor, 1))

    proc = subprocess.run(  # noqa: S603 -- runs the patched stub in bash
        ["/bin/bash", str(script)],
        capture_output=True,
        text=True,
        env={"ELB_BLAST_OPTIONS": blast_options, "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    return [
        line[len("ARG[") : -1]
        for line in proc.stdout.splitlines()
        if line.startswith("ARG[") and line.endswith("]")
    ]


def test_blast_run_argv_single_token_outfmt_is_byte_identical(tmp_path: Path) -> None:
    """For the single-token -outfmt every job uses today, the rebuilt argv is
    identical to plain word-splitting (no behavioural change)."""
    argv = _run_argv_rebuild(tmp_path, "-evalue 0.05 -outfmt 5 -word_size 28 -dust yes")
    assert argv == ["-evalue", "0.05", "-outfmt", "5", "-word_size", "28", "-dust", "yes"]


def test_blast_run_argv_multitoken_outfmt_is_grouped(tmp_path: Path) -> None:
    """A multi-token -outfmt is rejoined into ONE argv element so it reaches
    blastn intact (the whole point of the patch)."""
    argv = _run_argv_rebuild(
        tmp_path,
        "-evalue 0.05 -outfmt 7 sseqid staxids sstrand pident evalue bitscore -word_size 28",
    )
    assert argv == [
        "-evalue",
        "0.05",
        "-outfmt",
        "7 sseqid staxids sstrand pident evalue bitscore",
        "-word_size",
        "28",
    ]


def test_blast_run_argv_outfmt_at_end(tmp_path: Path) -> None:
    """A multi-token -outfmt as the final option is grouped to the end."""
    argv = _run_argv_rebuild(tmp_path, "-evalue 0.05 -outfmt 7 std staxids")
    assert argv == ["-evalue", "0.05", "-outfmt", "7 std staxids"]


def test_blast_run_argv_glob_metachar_not_expanded(tmp_path: Path, monkeypatch) -> None:
    """A glob metacharacter in the options must NOT expand to filenames.

    The rebuild splits with glob disabled, so a stray ``*`` stays literal even
    when matching files exist in the working directory.
    """
    # Create a file that `*` would match if globbing were active.
    (tmp_path / "WOULD_MATCH.txt").write_text("x")
    monkeypatch.chdir(tmp_path)
    argv = _run_argv_rebuild(tmp_path, "-evalue 0.05 -outfmt 7 -word_size *")
    assert argv == ["-evalue", "0.05", "-outfmt", "7", "-word_size", "*"]


def test_patch_blast_run_outfmt_argv_is_idempotent(tmp_path: Path) -> None:
    patch_module = _load_patch_module()
    script = tmp_path / "blast-run-aks.sh"
    script.write_text(_BLAST_RUN_AKS_STUB)
    patch_module.patch_blast_run_aks_outfmt_argv(script)
    once = script.read_text()
    patch_module.patch_blast_run_aks_outfmt_argv(script)
    assert script.read_text() == once
    assert once.count("ELB outfmt argv rebuild") == 1


def test_patch_init_shard_script_is_idempotent(tmp_path: Path) -> None:
    patch_module = _load_patch_module()
    script_path = tmp_path / "src" / "elastic_blast" / "templates" / "scripts"
    script_path.mkdir(parents=True)
    target = script_path / "init-db-shard-aks.sh"
    target.write_text("legacy\n")

    patch_module.patch_init_shard_script(tmp_path)
    once = target.read_text()
    patch_module.patch_init_shard_script(tmp_path)

    assert target.read_text() == once


def test_patch_init_shard_script_updates_installed_package_copy(tmp_path: Path) -> None:
    patch_module = _load_patch_module()
    source_dir = tmp_path / "src" / "elastic_blast" / "templates" / "scripts"
    installed_dir = (
        tmp_path
        / "venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "elastic_blast"
        / "templates"
        / "scripts"
    )
    source_dir.mkdir(parents=True)
    installed_dir.mkdir(parents=True)
    source_target = source_dir / "init-db-shard-aks.sh"
    installed_target = installed_dir / "init-db-shard-aks.sh"
    source_target.write_text("source legacy\n")
    installed_target.write_text("installed legacy\n")

    patch_module.patch_init_shard_script(tmp_path)

    for target in (source_target, installed_target):
        text = target.read_text()
        assert "Resolving DB metadata: ${METADATA_URL}" in text
        assert "DOWNLOAD_SKIP existing shard=${ELB_SHARD_IDX}" in text
        assert "source legacy" not in text
        assert "installed legacy" not in text


def _init_shard_runtime_assets(
    tmp_path: Path,
    *,
    manifest_text: str = "core_nt.00\n",
    schema: int = 1,
    layout_present: bool = True,
    digest_override: str | None = None,
    available_bytes: int = 110,
) -> tuple[Path, dict[str, str], Path]:
    patch_module = _load_patch_module()
    script_dir = tmp_path / "src" / "elastic_blast" / "templates" / "scripts"
    script_dir.mkdir(parents=True)
    script = script_dir / "init-db-shard-aks.sh"
    script.write_text("legacy\n")
    patch_module.patch_init_shard_script(tmp_path)

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    nal_text = "TITLE core_nt_shard_00\nDBLIST /blast/blastdb/core_nt.00\n"
    digest = digest_override or hashlib.sha256(f"{manifest_text}\0{nal_text}".encode()).hexdigest()
    (fixtures / "manifest").write_text(manifest_text)
    (fixtures / "nal").write_text(nal_text)
    (fixtures / "layout").write_text(f"{digest} 100\n")
    (fixtures / "metadata").write_text(
        json.dumps(
            {
                "source_version": "2026-08-25-00-00-00",
                "shard_layout_schema": schema,
            }
        )
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    azcopy = fake_bin / "azcopy"
    azcopy.write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        'if [ "$1" = "login" ]; then exit 0; fi\n'
        '[ "$1" = "cp" ]\n'
        'source_url="$2"\n'
        'destination="$3"\n'
        'printf "%s\\n" "$source_url" >> "$ELB_TEST_AZCOPY_CALLS"\n'
        'case "$source_url" in\n'
        '  *.manifest) cp "$ELB_TEST_MANIFEST" "$destination" ;;\n'
        '  *.nal) cp "$ELB_TEST_NAL" "$destination" ;;\n'
        "  *.layout)\n"
        '    [ "$ELB_TEST_LAYOUT_PRESENT" = "1" ] || exit 1\n'
        '    cp "$ELB_TEST_LAYOUT" "$destination"\n'
        "    ;;\n"
        '  *-metadata.json) cp "$ELB_TEST_METADATA" "$destination" ;;\n'
        "  *) printf x > core_nt.00.nsq ;;\n"
        "esac\n"
    )
    azcopy.chmod(0o755)
    (fake_bin / "blastdbcmd").write_text("#!/bin/bash\nexit 0\n")
    (fake_bin / "blastdbcmd").chmod(0o755)
    (fake_bin / "sleep").write_text("#!/bin/bash\nexit 0\n")
    (fake_bin / "sleep").chmod(0o755)
    (fake_bin / "df").write_text(
        "#!/bin/bash\nprintf 'Avail\\n%s\\n' \"$ELB_TEST_AVAILABLE_BYTES\"\n"
    )
    (fake_bin / "df").chmod(0o755)

    calls = tmp_path / "azcopy-calls"
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "ELB_SHARD_IDX": "00",
        "ELB_PARTITION_PREFIX": (
            "https://elbstg.blob.core.windows.net/blast-db/2shards/core_nt_shard_"
        ),
        "ELB_DB": "core_nt_shard_00",
        "ELB_DB_MOL_TYPE": "nucl",
        "ELB_BLASTDB_DIR": str(tmp_path),
        "ELB_STAGE_LOCK_TIMEOUT_SECONDS": "2",
        "ELB_STAGE_DISK_RESERVE_BYTES": "10",
        "ELB_TEST_MANIFEST": str(fixtures / "manifest"),
        "ELB_TEST_NAL": str(fixtures / "nal"),
        "ELB_TEST_LAYOUT": str(fixtures / "layout"),
        "ELB_TEST_METADATA": str(fixtures / "metadata"),
        "ELB_TEST_LAYOUT_PRESENT": "1" if layout_present else "0",
        "ELB_TEST_AVAILABLE_BYTES": str(available_bytes),
        "ELB_TEST_AZCOPY_CALLS": str(calls),
    }
    return script, env, calls


@pytest.mark.subprocess
def test_init_shard_disk_preflight_stops_payload_before_download(tmp_path: Path) -> None:
    script, env, calls = _init_shard_runtime_assets(tmp_path, available_bytes=109)

    result = subprocess.run(  # noqa: S603 -- executes generated fixture
        ["/bin/bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 28
    assert "DISK_PREFLIGHT required_bytes=100 reserve_bytes=10 available_bytes=109" in result.stdout
    assert "insufficient node-local disk" in result.stdout
    assert not any(call.endswith("/core_nt/*") for call in calls.read_text().splitlines())


@pytest.mark.subprocess
def test_init_shard_disk_preflight_allows_exact_boundary(tmp_path: Path) -> None:
    script, env, calls = _init_shard_runtime_assets(tmp_path, available_bytes=110)

    result = subprocess.run(  # noqa: S603 -- executes generated fixture
        ["/bin/bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert any(call.endswith("/core_nt/*") for call in calls.read_text().splitlines())
    assert (tmp_path / ".elb-cache.core_nt_shard_00.complete").read_text() == "ok"
    assert len((tmp_path / ".elb-cache.core_nt_shard_00.layout-sha256").read_text()) == 64


@pytest.mark.subprocess
@pytest.mark.parametrize(
    ("layout_present", "digest_override", "expected_error"),
    [
        (False, None, "schema 1 requires shard layout metadata"),
        (True, "0" * 64, "shard layout digest mismatch"),
    ],
)
def test_init_shard_schema_one_fails_closed_on_invalid_layout(
    tmp_path: Path,
    layout_present: bool,
    digest_override: str | None,
    expected_error: str,
) -> None:
    script, env, calls = _init_shard_runtime_assets(
        tmp_path,
        layout_present=layout_present,
        digest_override=digest_override,
    )

    result = subprocess.run(  # noqa: S603 -- executes generated fixture
        ["/bin/bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 65
    assert expected_error in result.stdout
    assert not any(call.endswith("/core_nt/*") for call in calls.read_text().splitlines())


@pytest.mark.subprocess
def test_init_shard_rejects_unsafe_manifest_before_payload_download(tmp_path: Path) -> None:
    script, env, calls = _init_shard_runtime_assets(
        tmp_path,
        manifest_text="../other-db\n",
    )

    result = subprocess.run(  # noqa: S603 -- executes generated fixture
        ["/bin/bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 65
    assert "invalid volume name in shard manifest" in result.stdout
    assert not any(call.endswith("/core_nt/*") for call in calls.read_text().splitlines())


@pytest.mark.subprocess
def test_init_shard_rejects_source_generation_change_before_payload_download(
    tmp_path: Path,
) -> None:
    script, env, calls = _init_shard_runtime_assets(tmp_path)
    env["ELB_DB_SOURCE_VERSION"] = "2026-08-24-00-00-00"
    alias = tmp_path / "core_nt_shard_00.nal"
    alias.write_text("existing validated alias\n")

    result = subprocess.run(  # noqa: S603 -- executes generated fixture
        ["/bin/bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 75
    assert "DB source version changed after Job creation" in result.stdout
    assert "expected=2026-08-24-00-00-00 actual=2026-08-25-00-00-00" in result.stdout
    assert not any(call.endswith("/core_nt/*") for call in calls.read_text().splitlines())
    assert alias.read_text() == "existing validated alias\n"


@pytest.mark.subprocess
def test_init_shard_layout_marker_mismatch_forces_payload_refresh(tmp_path: Path) -> None:
    script, env, calls = _init_shard_runtime_assets(tmp_path)

    initial = subprocess.run(  # noqa: S603 -- executes generated fixture
        ["/bin/bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert initial.returncode == 0, (initial.stdout, initial.stderr)
    (tmp_path / ".elb-cache.core_nt_shard_00.layout-sha256").write_text("0" * 64)
    calls.write_text("")

    refreshed = subprocess.run(  # noqa: S603 -- executes generated fixture
        ["/bin/bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert refreshed.returncode == 0, (refreshed.stdout, refreshed.stderr)
    assert "CACHE_STALE shard layout mismatch" in refreshed.stdout
    assert any(call.endswith("/core_nt/*") for call in calls.read_text().splitlines())


@pytest.mark.subprocess
def test_init_shard_rebuild_preserves_shared_taxonomy_assets(tmp_path: Path) -> None:
    script, env, _calls = _init_shard_runtime_assets(tmp_path)
    shared = {
        "taxdb.btd": "existing-btd",
        "taxdb.bti": "existing-bti",
        "taxonomy4blast.sqlite3": "existing-sqlite",
    }
    for name, value in shared.items():
        (tmp_path / name).write_text(value)

    result = subprocess.run(  # noqa: S603 -- executes generated fixture
        ["/bin/bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    for name, value in shared.items():
        assert (tmp_path / name).read_text() == value


def test_patch_aks_workload_tolerations_merges_existing_node_selector(
    tmp_path: Path,
) -> None:
    patch_module = _load_patch_module()
    templates = tmp_path / "src" / "elastic_blast" / "templates"
    templates.mkdir(parents=True)
    restart_policies = {
        "blast-batch-job-aks.yaml.template": "OnFailure",
        "blast-batch-job-local-ssd-aks.yaml.template": "OnFailure",
        "blast-batch-job-shard-ssd-aks.yaml.template": "OnFailure",
        "job-init-pv-aks.yaml.template": "Never",
        "job-init-pv-partitioned-aks.yaml.template": "Never",
        "job-init-local-ssd-aks.yaml.template": "Never",
        "job-init-ssd-shard-aks.yaml.template": "Never",
        "job-submit-jobs-aks.yaml.template": "Never",
        "vmtouch-daemonset-aks.yaml.template": "Always",
    }
    for name, restart_policy in restart_policies.items():
        (templates / name).write_text(
            f"      restartPolicy: {restart_policy}\n"
            "      nodeSelector:\n"
            '        ordinal: "${NODE_ORDINAL}"\n'
        )

    patch_module.patch_aks_workload_tolerations(tmp_path)
    snapshots = {path: path.read_text() for path in templates.iterdir()}
    patch_module.patch_aks_workload_tolerations(tmp_path)

    for path, text in snapshots.items():
        assert path.read_text() == text
        assert text.count("      nodeSelector:\n") == 1
        assert (
            '      nodeSelector:\n        workload: blast\n        ordinal: "${NODE_ORDINAL}"\n'
        ) in text


def test_patch_aks_workload_tolerations_repairs_duplicate_node_selector(
    tmp_path: Path,
) -> None:
    patch_module = _load_patch_module()
    templates = tmp_path / "src" / "elastic_blast" / "templates"
    templates.mkdir(parents=True)
    names = {
        "blast-batch-job-aks.yaml.template": "OnFailure",
        "blast-batch-job-local-ssd-aks.yaml.template": "OnFailure",
        "blast-batch-job-shard-ssd-aks.yaml.template": "OnFailure",
        "job-init-pv-aks.yaml.template": "Never",
        "job-init-pv-partitioned-aks.yaml.template": "Never",
        "job-init-local-ssd-aks.yaml.template": "Never",
        "job-init-ssd-shard-aks.yaml.template": "Never",
        "job-submit-jobs-aks.yaml.template": "Never",
        "vmtouch-daemonset-aks.yaml.template": "Always",
    }
    duplicate = (
        "      tolerations:\n"
        "      - key: workload\n"
        "        operator: Equal\n"
        "        value: blast\n"
        "        effect: NoSchedule\n"
        "      nodeSelector:\n"
        "        workload: blast\n"
        "      nodeSelector:\n"
        '        ordinal: "${NODE_ORDINAL}"\n'
    )
    for name in names:
        (templates / name).write_text(duplicate)

    patch_module.patch_aks_workload_tolerations(tmp_path)

    for path in templates.iterdir():
        text = path.read_text()
        assert text.count("      nodeSelector:\n") == 1
        assert "        workload: blast\n" in text
        assert '        ordinal: "${NODE_ORDINAL}"\n' in text


def test_patch_azure_traits_adds_dashboard_v7_skus(tmp_path: Path) -> None:
    patch_module = _load_patch_module()
    source_dir = tmp_path / "src" / "elastic_blast"
    installed_dir = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "elastic_blast"
    source_dir.mkdir(parents=True)
    installed_dir.mkdir(parents=True)
    base_text = (
        "AZURE_HPC_MACHINES = {\n"
        "    'Standard_D8s_v3': {'cpu': 8, 'memory': 32},  # 8 vCPU, 32 GB RAM\n"
        "}\n"
        "AZURE_VM_HOURLY_PRICES = {\n"
        "    'Standard_D64s_v3': 3.072,\n"
        "}\n"
    )
    for target in (source_dir / "azure_traits.py", installed_dir / "azure_traits.py"):
        target.write_text(base_text)

    patch_module.patch_azure_traits(tmp_path)
    patch_module.patch_azure_traits(tmp_path)

    for target in (source_dir / "azure_traits.py", installed_dir / "azure_traits.py"):
        text = target.read_text()
        assert text.count("Standard_E32as_v7") == 2
        assert "'Standard_E32as_v7': {'cpu': 32, 'memory': 256}" in text
        assert "'Standard_D2as_v7': {'cpu': 2, 'memory': 8}" in text
        assert "'Standard_E48as_v7': 3.024" in text


def test_patch_azure_cli_glue_clears_cleanup_stack_for_json_submit_success(
    tmp_path: Path,
) -> None:
    patch_module = _load_patch_module()
    target_dir = tmp_path / "src" / "elastic_blast"
    target_dir.mkdir(parents=True)
    target = target_dir / "azure_cli_glue.py"
    target.write_text(
        "def submit_command(args, cfg, clean_up_stack, *, default_submit):\n"
        "    rc = default_submit(args, cfg, clean_up_stack)\n"
        "    # Phase 3: success -> structured ACCEPTED.\n"
        "    if json_mode and rc == 0:\n"
        "        result = SubmitResult(\n"
        "            decision=SubmitDecision.ACCEPTED,\n"
        "            correlation_id=correlation_id,\n"
        "            cluster_name=cfg.cluster.name,\n"
        "            message='submission accepted')\n"
        "        emit_json(_wrap_submit_result(result))\n"
        "    return rc\n"
    )

    patch_module.patch_azure_cli_glue(tmp_path)
    once = target.read_text()
    patch_module.patch_azure_cli_glue(tmp_path)

    assert target.read_text() == once
    assert "Dashboard JSON submit has its own log/state collectors" in once
    assert "clean_up_stack.clear()" in once
    assert once.index("clean_up_stack.clear()") < once.index("result = SubmitResult(")


_CREATE_WORKSPACE_DAEMONSET_TEMPLATE = """---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: create-workspace
  namespace: kube-system
spec:
  template:
    spec:
      containers:
      - name: create-dir
        image: busybox
      volumes:
      - name: host-workspace
        hostPath:
          path: /workspace
          type: DirectoryOrCreate
      nodeSelector:
        kubernetes.io/os: linux

---
apiVersion: batch/v1
kind: Job
metadata:
  name: init-ssd-${BLAST_ELB_JOB_ID_SHORT}-${NODE_ORDINAL}
spec:
  template:
    spec:
      restartPolicy: Never
      tolerations:
      - key: workload
        operator: Equal
        value: blast
        effect: NoSchedule
      nodeSelector:
        workload: blast
"""


def _write_create_workspace_templates(root: Path) -> list[Path]:
    template_dir = root / "src" / "elastic_blast" / "templates"
    template_dir.mkdir(parents=True)
    paths = []
    for name in (
        "job-init-local-ssd-aks.yaml.template",
        "job-init-ssd-shard-aks.yaml.template",
    ):
        path = template_dir / name
        path.write_text(_CREATE_WORKSPACE_DAEMONSET_TEMPLATE)
        paths.append(path)
    return paths


def test_patch_create_workspace_daemonset_tolerations_adds_blast_toleration(
    tmp_path: Path,
) -> None:
    patch_module = _load_patch_module()
    paths = _write_create_workspace_templates(tmp_path)

    patch_module.patch_create_workspace_daemonset_tolerations(tmp_path)

    expected_block = (
        "          type: DirectoryOrCreate\n"
        "      tolerations:\n"
        "      - key: workload\n"
        "        operator: Equal\n"
        "        value: blast\n"
        "        effect: NoSchedule\n"
        "      nodeSelector:\n"
        "        kubernetes.io/os: linux\n"
    )
    for path in paths:
        text = path.read_text()
        # DaemonSet now tolerates the blast pool taint.
        assert expected_block in text
        # The Job below the DaemonSet still keeps its own workload nodeSelector
        # and toleration - we did not touch it.
        assert "        workload: blast\n" in text
        # Patch only injects one toleration block (DaemonSet); the Job already
        # had one, so the file ends with two toleration occurrences total.
        assert text.count("- key: workload\n") == 2


def test_patch_create_workspace_daemonset_tolerations_is_idempotent(
    tmp_path: Path,
) -> None:
    patch_module = _load_patch_module()
    paths = _write_create_workspace_templates(tmp_path)

    patch_module.patch_create_workspace_daemonset_tolerations(tmp_path)
    snapshots = {path: path.read_text() for path in paths}
    patch_module.patch_create_workspace_daemonset_tolerations(tmp_path)

    for path in paths:
        assert path.read_text() == snapshots[path]


# ---------------------------------------------------------------------------
# patch_blast_run_aks_script — vmtouch warm step inside the BLAST search pod
# ---------------------------------------------------------------------------

_BLAST_RUN_AKS_LEGACY = (
    "#!/bin/bash\n"
    "# blast-run-aks.sh — Execute BLAST search\n"
    'echo "BASH version ${BASH_VERSION}"\n'
    'ELB_DB_SAFE="${ELB_DB//\\//-}"\n'
    "BLAST_RUNTIME=$(mktemp)\n"
    "ERROR_FILE=$(mktemp)\n"
    'DATE_NOW=$(date -u +"$ELB_TIMEFMT")\n'
    'if [[ ! -s "$RESULTS_DIR/BLASTDB_LENGTH.out" ]]; then\n'
    '    blastdbcmd -info -db "$ELB_DB" \\\n'
    "    | awk '/total/ {print $3}' \\\n"
    '    | tr -d , > "$RESULTS_DIR/BLASTDB_LENGTH.out"\n'
    "fi\n"
    "\n"
    "start=$(date +%s)\n"
    'echo "run start $JOB_NUM $ELB_BLAST_PROGRAM $ELB_DB"\n'
    "$ELB_BLAST_PROGRAM \\\n"
    '-db "$ELB_DB" \\\n'
    '-query "$QUERY_DIR/batch_${JOB_NUM}.fa" \\\n'
    '-num_threads "$ELB_NUM_CPUS" \\\n'
    "$ELB_BLAST_OPTIONS\n"
    "BLAST_EXIT_CODE=$?\n"
    "exit $BLAST_EXIT_CODE\n"
)


def _write_blast_run_aks_script(tmp_path: Path) -> list[Path]:
    source_dir = tmp_path / "src" / "elastic_blast" / "templates" / "scripts"
    installed_dir = (
        tmp_path
        / "venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "elastic_blast"
        / "templates"
        / "scripts"
    )
    source_dir.mkdir(parents=True)
    installed_dir.mkdir(parents=True)
    paths = [
        source_dir / "blast-run-aks.sh",
        installed_dir / "blast-run-aks.sh",
    ]
    for path in paths:
        path.write_text(_BLAST_RUN_AKS_LEGACY)
    return paths


def test_patch_blast_run_aks_script_injects_vmtouch_before_blastn(tmp_path: Path) -> None:
    patch_module = _load_patch_module()
    paths = _write_blast_run_aks_script(tmp_path)

    patch_module.patch_blast_run_aks_script(tmp_path)

    for path in paths:
        text = path.read_text()
        # Marker comment is present so the patch can detect re-runs.
        assert "ELB vmtouch warm step" in text
        # The vmtouch call uses blastdb_path to enumerate volume files, the
        # serial `-t` (touch only) mode, and a memory budget driven by
        # MemAvailable. We must NOT use `-l` (mlock) because the warmup pod
        # exits immediately and BLAST's own mmap is what keeps the pages
        # resident.
        assert "blastdb_path -dbtype" in text
        assert "vmtouch -tqm" in text
        assert "vmtouch -l " not in text
        assert "vmtouch -d " not in text
        # Failure must not abort the BLAST search — vmtouch is a best-effort
        # warm step, the search must still run if vmtouch is missing on the
        # node or the volume list is empty.
        assert "|| true" in text
        # The step is inserted ABOVE the existing `start=$(date +%s)` /
        # `echo "run start"` block (i.e. before BLAST is invoked), not after.
        block_idx = text.index("ELB vmtouch warm step")
        run_start_idx = text.index('echo "run start')
        blastn_idx = text.index("$ELB_BLAST_PROGRAM \\")
        assert block_idx < run_start_idx < blastn_idx
        # The ELB_VMTOUCH_DISABLE escape hatch is documented + actually used.
        assert "ELB_VMTOUCH_DISABLE" in text
        # The RUNTIME metric line is emitted so the result-export step picks
        # it up alongside the existing blast-job-NNN runtime line.
        assert "printf 'RUNTIME %s %f seconds" in text
        # The runtime line is captured once via printf into a shell variable
        # and then echoed twice: once to stdout (pod log) and once appended
        # to $BLAST_RUNTIME so results-export-aks.sh ships it to Blob via
        # the existing BLAST_RUNTIME-${JOB_NUM}.out upload. The SPA
        # surfacing follow-up depends on that artefact being present.
        assert "vm_runtime_line=" in text
        assert text.count('echo "$vm_runtime_line"') == 2
        assert '>> "$BLAST_RUNTIME"' in text
        assert "ELB DB reader lock" in text
        assert 'flock -s -w "$READER_LOCK_WAIT_SECONDS" 8' in text
        assert 'READER_LOCK_FILE=".elb-stage.lock"' in text
        assert "DB_READER_LOCK_RELEASED" in text
        assert "DB_READER_CACHE_VERIFIED" in text
        assert "DB shard completion marker is missing" in text
        assert text.index("DB_READER_LOCK_ACQUIRED") < text.index("blastdbcmd -info -db")
        assert text.index("DB_READER_CACHE_VERIFIED") < text.index("blastdbcmd -info -db")
        assert text.index("BLAST_EXIT_CODE=$?") < text.index("DB_READER_LOCK_RELEASED")


def test_patch_blast_run_aks_script_is_idempotent(tmp_path: Path) -> None:
    patch_module = _load_patch_module()
    paths = _write_blast_run_aks_script(tmp_path)

    patch_module.patch_blast_run_aks_script(tmp_path)
    snapshots = {path: path.read_text() for path in paths}
    patch_module.patch_blast_run_aks_script(tmp_path)

    for path in paths:
        text = path.read_text()
        assert text == snapshots[path]
        # The vmtouch block appears exactly once even after re-running.
        assert text.count("ELB vmtouch warm step") == 1
        assert text.count("ELB DB reader lock") == 1
        assert text.count("DB_READER_LOCK_RELEASED") == 1


def test_patch_blast_run_aks_script_updates_installed_package_copy(tmp_path: Path) -> None:
    patch_module = _load_patch_module()
    paths = _write_blast_run_aks_script(tmp_path)

    patch_module.patch_blast_run_aks_script(tmp_path)

    for path in paths:
        text = path.read_text()
        assert "ELB vmtouch warm step" in text
        # Original script content (legacy header) is preserved.
        assert "blast-run-aks.sh — Execute BLAST search" in text


def test_patch_blast_run_aks_script_missing_anchor_raises(tmp_path: Path) -> None:
    patch_module = _load_patch_module()
    source_dir = tmp_path / "src" / "elastic_blast" / "templates" / "scripts"
    source_dir.mkdir(parents=True)
    target = source_dir / "blast-run-aks.sh"
    # A script without the upstream anchor must fail loudly rather than
    # silently producing a half-patched file.
    target.write_text("#!/bin/bash\necho hi\n")

    import pytest

    with pytest.raises(RuntimeError, match="expected one match"):
        patch_module.patch_blast_run_aks_script(tmp_path)


def test_patch_sharded_reader_lock_opt_in_changes_both_local_ssd_templates(
    tmp_path: Path,
) -> None:
    patch_module = _load_patch_module()
    templates = tmp_path / "src" / "elastic_blast" / "templates"
    templates.mkdir(parents=True)
    env_block = (
        "        - name: ELB_DB_MOL_TYPE\n"
        '          value: "${ELB_DB_MOL_TYPE}"\n'
        "        - name: QUERY_DIR\n"
    )
    sharded = templates / "blast-batch-job-shard-ssd-aks.yaml.template"
    persistent = templates / "blast-batch-job-aks.yaml.template"
    local = templates / "blast-batch-job-local-ssd-aks.yaml.template"
    for path in (sharded, persistent, local):
        path.write_text(env_block)

    patch_module.patch_sharded_reader_lock_opt_in(tmp_path)
    once = sharded.read_text()
    patch_module.patch_sharded_reader_lock_opt_in(tmp_path)

    assert sharded.read_text() == once
    assert 'name: ELB_DB_READER_LOCK\n          value: "1"' in once
    assert "ELB_DB_READER_LOCK" not in persistent.read_text()
    assert 'name: ELB_DB_READER_LOCK\n          value: "1"' in local.read_text()


def test_patch_init_db_download_writer_lock_is_local_ssd_opt_in(
    tmp_path: Path,
) -> None:
    patch_module = _load_patch_module()
    scripts = tmp_path / "src" / "elastic_blast" / "templates" / "scripts"
    scripts.mkdir(parents=True)
    target = scripts / "init-db-download-aks.sh"
    target.write_text(
        "#!/bin/bash\n"
        'if [ -n "${STARTUP_DELAY:-}" ]; then\n'
        "    :\n"
        "fi\n\n"
        "start=$(date +%s)\n"
        "# Clean up azcopy background processes to ensure container exits cleanly.\n"
    )
    template = scripts.parent / "job-init-local-ssd-aks.yaml.template"
    template.write_text(
        "        - name: ELB_SKIP_DB_VERIFY\n"
        '          value: "true"\n'
        "        - name: STARTUP_DELAY\n"
    )

    patch_module.patch_init_db_download_writer_lock(tmp_path)
    script_once = target.read_text()
    template_once = template.read_text()
    patch_module.patch_init_db_download_writer_lock(tmp_path)

    assert target.read_text() == script_once
    assert template.read_text() == template_once
    assert "ELB DB writer lock" in script_once
    assert 'if [ "${ELB_DB_WRITER_LOCK:-0}" = "1" ]; then' in script_once
    assert 'flock -w "$STAGE_LOCK_WAIT_SECONDS" 9' in script_once
    assert "exit 75" in script_once
    assert "local-SSD DB writer integrity check failed" in script_once
    assert "DB_WRITER_CACHE_VERIFIED" in script_once
    assert script_once.index("DB_WRITER_CACHE_VERIFIED") < script_once.index(
        "# Clean up azcopy background processes"
    )
    assert 'name: ELB_DB_WRITER_LOCK\n          value: "1"' in template_once


def _reader_lock_test_assets(tmp_path: Path) -> tuple[Path, Path, Path]:
    paths = _write_blast_run_aks_script(tmp_path)
    patch_module = _load_patch_module()
    patch_module.patch_blast_run_aks_script(tmp_path)
    results = tmp_path / "results"
    queries = tmp_path / "queries"
    results.mkdir()
    queries.mkdir()
    (results / "BLASTDB_LENGTH.out").write_text("100\n")
    program = tmp_path / "blast-reader"
    program.write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        ': > "$READER_READY"\n'
        'while [ ! -e "$READER_RELEASE" ]; do sleep 0.02; done\n'
    )
    program.chmod(0o755)
    blastdbcmd = tmp_path / "blastdbcmd"
    blastdbcmd.write_text('#!/bin/bash\nexit "${BLASTDBCMD_EXIT:-0}"\n')
    blastdbcmd.chmod(0o755)
    (tmp_path / ".elb-cache.core_nt_shard_00.complete").write_text("ok")
    return paths[0], results, queries


def _reader_lock_env(
    *,
    program: Path,
    results: Path,
    queries: Path,
    ready: Path,
    release: Path,
    timeout_seconds: str = "5",
) -> dict[str, str]:
    return {
        "PATH": f"{program.parent}:/usr/bin:/bin",
        "ELB_DB_READER_LOCK": "1",
        "ELB_DB_READER_LOCK_TIMEOUT_SECONDS": timeout_seconds,
        "ELB_VMTOUCH_DISABLE": "1",
        "ELB_BLAST_PROGRAM": str(program),
        "ELB_DB": "core_nt_shard_00",
        "ELB_DB_MOL_TYPE": "nucl",
        "ELB_NUM_CPUS": "1",
        "ELB_BLAST_OPTIONS": "",
        "ELB_TIMEFMT": "%s",
        "JOB_NUM": "000",
        "QUERY_DIR": str(queries),
        "RESULTS_DIR": str(results),
        "READER_READY": str(ready),
        "READER_RELEASE": str(release),
    }


def _wait_for_reader(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(
                f"reader exited before acquiring lock: rc={process.returncode} "
                f"stdout={stdout!r} stderr={stderr!r}"
            )
        time.sleep(0.02)
    assert path.exists(), "reader did not acquire the shared lock"


def _writer_lock_available(lock_file: Path) -> bool:
    with lock_file.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True


@pytest.mark.subprocess
def test_blast_reader_locks_coexist_and_exclude_writer(tmp_path: Path) -> None:
    script, results, queries = _reader_lock_test_assets(tmp_path)
    program = tmp_path / "blast-reader"
    readers: list[subprocess.Popen[str]] = []
    releases = [tmp_path / "release-1", tmp_path / "release-2"]
    try:
        for index in range(2):
            ready = tmp_path / f"ready-{index + 1}"
            process = subprocess.Popen(  # noqa: S603 -- executes generated fixture
                ["/bin/bash", str(script)],
                cwd=tmp_path,
                env=_reader_lock_env(
                    program=program,
                    results=results,
                    queries=queries,
                    ready=ready,
                    release=releases[index],
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            readers.append(process)
            _wait_for_reader(ready, process)

        lock_file = tmp_path / ".elb-stage.lock"
        assert not _writer_lock_available(lock_file)

        releases[0].touch()
        stdout, stderr = readers[0].communicate(timeout=5)
        assert readers[0].returncode == 0, (stdout, stderr)
        assert not _writer_lock_available(lock_file)

        releases[1].touch()
        stdout, stderr = readers[1].communicate(timeout=5)
        assert readers[1].returncode == 0, (stdout, stderr)
        assert _writer_lock_available(lock_file)
    finally:
        for release in releases:
            release.touch(exist_ok=True)
        for process in readers:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)


@pytest.mark.subprocess
def test_blast_reader_lock_timeout_is_bounded(tmp_path: Path) -> None:
    script, results, queries = _reader_lock_test_assets(tmp_path)
    lock_file = tmp_path / ".elb-stage.lock"
    with lock_file.open("a+") as writer:
        fcntl.flock(writer.fileno(), fcntl.LOCK_EX)
        started = time.monotonic()
        result = subprocess.run(  # noqa: S603 -- executes generated fixture
            ["/bin/bash", str(script)],
            cwd=tmp_path,
            env=_reader_lock_env(
                program=tmp_path / "blast-reader",
                results=results,
                queries=queries,
                ready=tmp_path / "never-ready",
                release=tmp_path / "never-release",
                timeout_seconds="1",
            ),
            capture_output=True,
            text=True,
            timeout=5,
        )
        elapsed = time.monotonic() - started
        fcntl.flock(writer.fileno(), fcntl.LOCK_UN)

    assert result.returncode == 75
    assert "DB reader lock timeout" in result.stderr
    assert 0.8 <= elapsed < 4


@pytest.mark.subprocess
def test_blast_reader_fails_closed_when_locked_cache_is_corrupt(tmp_path: Path) -> None:
    script, results, queries = _reader_lock_test_assets(tmp_path)
    ready = tmp_path / "never-ready"
    env = _reader_lock_env(
        program=tmp_path / "blast-reader",
        results=results,
        queries=queries,
        ready=ready,
        release=tmp_path / "never-release",
    )
    env["BLASTDBCMD_EXIT"] = "1"

    result = subprocess.run(  # noqa: S603 -- executes generated fixture
        ["/bin/bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 76
    assert "DB cache integrity check failed after reader lock acquisition" in result.stderr
    assert not ready.exists()
    assert _writer_lock_available(tmp_path / ".elb-stage.lock")


@pytest.mark.subprocess
def test_blast_reader_fails_closed_without_shard_completion_marker(tmp_path: Path) -> None:
    script, results, queries = _reader_lock_test_assets(tmp_path)
    (tmp_path / ".elb-cache.core_nt_shard_00.complete").unlink()
    ready = tmp_path / "never-ready"

    result = subprocess.run(  # noqa: S603 -- executes generated fixture
        ["/bin/bash", str(script)],
        cwd=tmp_path,
        env=_reader_lock_env(
            program=tmp_path / "blast-reader",
            results=results,
            queries=queries,
            ready=ready,
            release=tmp_path / "never-release",
        ),
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 76
    assert "DB shard completion marker is missing" in result.stderr
    assert not ready.exists()
    assert _writer_lock_available(tmp_path / ".elb-stage.lock")


def test_finalizer_awk_filter_preserves_fields_header() -> None:
    """The patched finalizer concatenation keeps the `# Fields:` comment so the
    merge can re-emit a self-describing header.

    Upstream uses ``awk '!/^#/'`` which drops every comment, including the
    authoritative ``# Fields:`` line that names the extended outfmt 7 columns
    (staxids / sscinames). The patch widens it to ``awk '/^# Fields:/ || !/^#/'``
    so the Fields line survives while other comment noise is still stripped.
    """
    shard_output = "\n".join(
        [
            "# BLASTN 2.17.0+",
            "# Query: q1",
            "# Database: core_nt_shard_00",
            (
                "# Fields: query acc.ver, subject acc.ver, % identity, "
                "alignment length, mismatches, gap opens, q. start, q. end, "
                "s. start, s. end, evalue, bit score, subject tax ids, "
                "subject sci names"
            ),
            "# 1 hits found",
            "q1\tPQ221797.1\t100.000\t462\t0\t0\t1\t462\t1\t462\t0.0\t828\t10244\tMonkeypox virus",
            "# BLAST processed 1 queries",
        ]
    )

    patched = subprocess.run(
        ["/bin/bash", "-c", "awk '/^# Fields:/ || !/^#/'"],
        input=shard_output,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    # The authoritative Fields header and the data row survive; other comment
    # lines (# BLASTN, # Query, # Database, # N hits found, # BLAST processed)
    # are stripped.
    assert any(line.startswith("# Fields:") for line in patched)
    assert "subject tax ids" in patched[0]
    assert any("Monkeypox virus" in line for line in patched)
    assert not any(line.startswith("# BLASTN") for line in patched)
    assert not any(line.startswith("# Query") for line in patched)
    assert not any(line.startswith("# 1 hits found") for line in patched)


def test_patch_finalizer_script_widens_awk_comment_filter(tmp_path: Path) -> None:
    """`patch_finalizer_script` rewrites the comment-stripping awk so the
    `# Fields:` header is preserved, and is idempotent."""
    patch_module = _load_patch_module()
    script_dir = tmp_path / "src" / "elastic_blast" / "templates" / "scripts"
    script_dir.mkdir(parents=True)
    finalizer = script_dir / "elb-finalizer-aks.sh"
    # Minimal fixture carrying only the awk anchor this assertion targets, at
    # the exact upstream indentation (20 spaces) so the replacement matches.
    finalizer.write_text(
        "#!/bin/bash\n"
        'for f in "$LOCAL_DIR"/*.out.gz; do\n'
        '                    if ! zcat "$f" | awk \'!/^#/\' >> "$MERGE_INPUT"; then\n'
        '                        echo "ERROR"\n'
        "                    fi\n"
        "done\n"
    )

    anchor = '                    if ! zcat "$f" | awk \'!/^#/\' >> "$MERGE_INPUT"; then\n'
    replacement = (
        "                    if ! zcat \"$f\" | awk '/^# Fields:/ || !/^#/' "
        '>> "$MERGE_INPUT"; then\n'
    )
    patch_module._replace_once_unless_present(
        finalizer, anchor, replacement, "awk '/^# Fields:/ || !/^#/'"
    )
    once = finalizer.read_text()
    # Idempotent: the marker short-circuits a second application.
    patch_module._replace_once_unless_present(
        finalizer, anchor, replacement, "awk '/^# Fields:/ || !/^#/'"
    )
    assert finalizer.read_text() == once
    assert "awk '/^# Fields:/ || !/^#/'" in once
    assert "awk '!/^#/'" not in once


def test_patch_source_wires_finalizer_awk_fields_preservation() -> None:
    """Guard the patch wiring: the finalizer patch must replace the upstream
    comment-stripping awk with the Fields-preserving form."""
    patch_path = Path(__file__).resolve().parents[2] / "terminal" / "patch_elastic_blast.py"
    source = patch_path.read_text()
    assert "awk '/^# Fields:/ || !/^#/'" in source


_BATCH_JOB_TEMPLATES = (
    ("blast-batch-job-aks.yaml.template", "  backoffLimit: 5\n"),
    ("blast-batch-job-local-ssd-aks.yaml.template", "  backoffLimit: 3\n"),
    ("blast-batch-job-shard-ssd-aks.yaml.template", "  backoffLimit: 3\n"),
    ("elb-finalizer-aks.yaml.template", "  backoffLimit: 0\n"),
)


def _write_batch_job_templates(tmp_path: Path) -> Path:
    templates_dir = tmp_path / "src" / "elastic_blast" / "templates"
    templates_dir.mkdir(parents=True)
    for name, backoff in _BATCH_JOB_TEMPLATES:
        (templates_dir / name).write_text(
            "---\n"
            "apiVersion: batch/v1\n"
            "kind: Job\n"
            "spec:\n"
            "  template:\n"
            "    spec:\n"
            "      restartPolicy: OnFailure\n"
            f"{backoff}"
        )
    return templates_dir


def test_patch_aks_job_ttl_injects_default_ttl(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ELB_JOB_TTL_SECONDS", raising=False)
    patch_module = _load_patch_module()
    templates_dir = _write_batch_job_templates(tmp_path)

    patch_module.patch_aks_job_ttl(tmp_path)

    for name, _ in _BATCH_JOB_TEMPLATES:
        text = (templates_dir / name).read_text()
        # Injected at Job.spec level (2-space indent), before backoffLimit.
        assert "\n  ttlSecondsAfterFinished: 1800\n" in text
        assert text.index("ttlSecondsAfterFinished") < text.index("backoffLimit")


def test_patch_aks_job_ttl_honors_env_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ELB_JOB_TTL_SECONDS", "600")
    patch_module = _load_patch_module()
    templates_dir = _write_batch_job_templates(tmp_path)

    patch_module.patch_aks_job_ttl(tmp_path)

    text = (templates_dir / "blast-batch-job-aks.yaml.template").read_text()
    assert "  ttlSecondsAfterFinished: 600\n" in text


def test_patch_aks_job_ttl_rejects_non_numeric_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ELB_JOB_TTL_SECONDS", "forever")
    patch_module = _load_patch_module()
    templates_dir = _write_batch_job_templates(tmp_path)

    patch_module.patch_aks_job_ttl(tmp_path)

    text = (templates_dir / "blast-batch-job-aks.yaml.template").read_text()
    assert "  ttlSecondsAfterFinished: 1800\n" in text


def test_patch_aks_job_ttl_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ELB_JOB_TTL_SECONDS", raising=False)
    patch_module = _load_patch_module()
    templates_dir = _write_batch_job_templates(tmp_path)

    patch_module.patch_aks_job_ttl(tmp_path)
    once = (templates_dir / "blast-batch-job-aks.yaml.template").read_text()
    patch_module.patch_aks_job_ttl(tmp_path)
    twice = (templates_dir / "blast-batch-job-aks.yaml.template").read_text()

    assert once == twice
    assert once.count("ttlSecondsAfterFinished") == 1


def test_patch_aks_job_ttl_anchors_on_real_field_not_comment(tmp_path: Path, monkeypatch) -> None:
    """A prose comment mentioning 'backoffLimit' must NOT be the insertion anchor.

    Mirrors the real `elb-finalizer-aks` template, which carries a comment block
    ("K8s default backoffLimit of 6 ... Pin to 0.") directly above the real
    `backoffLimit: 0` field. The TTL line must land before the real field, and
    the comment must survive intact.
    """
    monkeypatch.delenv("ELB_JOB_TTL_SECONDS", raising=False)
    patch_module = _load_patch_module()
    templates_dir = _write_batch_job_templates(tmp_path)
    # Overwrite the finalizer with the real comment-block structure: a prose
    # comment that mentions "backoffLimit" sits directly above the real field.
    tmpl = templates_dir / "elb-finalizer-aks.yaml.template"
    tmpl.write_text(
        "---\n"
        "apiVersion: batch/v1\n"
        "kind: Job\n"
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      restartPolicy: Never\n"
        "  # The finalizer is NOT safely retryable. K8s default backoffLimit\n"
        "  # of 6 would amplify the problem. Pin to 0.\n"
        "  backoffLimit: 0\n"
    )

    patch_module.patch_aks_job_ttl(tmp_path)
    text = tmpl.read_text()

    # Inserted immediately before the REAL field, not into the comment block.
    assert "  ttlSecondsAfterFinished: 1800\n  backoffLimit: 0\n" in text
    assert "of 6 would amplify the problem" in text  # comment survived
    import yaml

    doc = yaml.safe_load(text)
    assert doc["spec"]["ttlSecondsAfterFinished"] == 1800
    assert doc["spec"]["backoffLimit"] == 0
