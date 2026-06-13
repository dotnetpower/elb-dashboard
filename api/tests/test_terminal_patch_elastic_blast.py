"""Tests for Terminal Patch Elastic BLAST behavior.

Responsibility: Tests for Terminal Patch Elastic BLAST behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_load_patch_module`,
`test_patch_init_shard_script_writes_hardened_cache_skip`,
`test_patch_init_shard_script_is_idempotent`,
`test_patch_init_shard_script_updates_installed_package_copy`,
`test_patch_azure_traits_adds_dashboard_v7_skus`,
`test_patch_azure_cli_glue_clears_cleanup_stack_for_json_submit_success`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_terminal_patch_elastic_blast.py`.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


def _load_patch_module():
    module_path = Path(__file__).resolve().parents[2] / "terminal" / "patch_elastic_blast.py"
    spec = importlib.util.spec_from_file_location("terminal_patch_elastic_blast", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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
    assert "[ -f .download-complete ]" in text
    assert "TAXDB_SKIP taxdb files not present in DB prefix" in text
    assert "CACHE_INCOMPLETE missing ${volume}.${payload_ext}" in text
    assert "CACHE_STALE source-version mismatch" in text
    assert "Resolving DB source version: ${METADATA_URL}" in text
    assert "${DB_BASE_URL}${ORIG_DB}-metadata.json" in text
    assert "WARNING: DB metadata source-version lookup failed" in text
    assert "write_volpaths" in text
    assert "printf '%s' ok > .download-complete" in text
    assert "printf '%s' \"$EXPECTED_SOURCE_VERSION\" > .download-source-version" in text
    assert "if [ -s .download-complete ]" not in text
    assert "touch .download-complete" not in text
    assert "taxonomy4blast.sqlite3" not in skip_prefix


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
        assert "Resolving DB source version: ${METADATA_URL}" in text
        assert "DOWNLOAD_SKIP existing shard=${ELB_SHARD_IDX}" in text
        assert "source legacy" not in text
        assert "installed legacy" not in text


def test_patch_azure_traits_adds_dashboard_v7_skus(tmp_path: Path) -> None:
    patch_module = _load_patch_module()
    source_dir = tmp_path / "src" / "elastic_blast"
    installed_dir = (
        tmp_path
        / "venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "elastic_blast"
    )
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
    "echo \"BASH version ${BASH_VERSION}\"\n"
    "ELB_DB_SAFE=\"${ELB_DB//\\//-}\"\n"
    "BLAST_RUNTIME=$(mktemp)\n"
    "ERROR_FILE=$(mktemp)\n"
    "DATE_NOW=$(date -u +\"$ELB_TIMEFMT\")\n"
    "if [[ ! -s \"$RESULTS_DIR/BLASTDB_LENGTH.out\" ]]; then\n"
    "    blastdbcmd -info -db \"$ELB_DB\" \\\n"
    "    | awk '/total/ {print $3}' \\\n"
    "    | tr -d , > \"$RESULTS_DIR/BLASTDB_LENGTH.out\"\n"
    "fi\n"
    "\n"
    "start=$(date +%s)\n"
    "echo \"run start $JOB_NUM $ELB_BLAST_PROGRAM $ELB_DB\"\n"
    "$ELB_BLAST_PROGRAM \\\n"
    "-db \"$ELB_DB\" \\\n"
    "-query \"$QUERY_DIR/batch_${JOB_NUM}.fa\" \\\n"
    "-num_threads \"$ELB_NUM_CPUS\" \\\n"
    "$ELB_BLAST_OPTIONS\n"
    "exit $?\n"
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
        assert 'printf \'RUNTIME %s %f seconds' in text
        # The runtime line is captured once via printf into a shell variable
        # and then echoed twice: once to stdout (pod log) and once appended
        # to $BLAST_RUNTIME so results-export-aks.sh ships it to Blob via
        # the existing BLAST_RUNTIME-${JOB_NUM}.out upload. The SPA
        # surfacing follow-up depends on that artefact being present.
        assert "vm_runtime_line=" in text
        assert text.count('echo "$vm_runtime_line"') == 2
        assert '>> "$BLAST_RUNTIME"' in text


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
        "                    if ! zcat \"$f\" | awk '!/^#/' >> \"$MERGE_INPUT\"; then\n"
        '                        echo "ERROR"\n'
        "                    fi\n"
        "done\n"
    )

    anchor = "                    if ! zcat \"$f\" | awk '!/^#/' >> \"$MERGE_INPUT\"; then\n"
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
    patch_path = (
        Path(__file__).resolve().parents[2] / "terminal" / "patch_elastic_blast.py"
    )
    source = patch_path.read_text()
    assert "awk '/^# Fields:/ || !/^#/'" in source


