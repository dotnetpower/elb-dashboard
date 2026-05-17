#!/usr/bin/env python3
"""Patch the vendored elastic-blast-azure clone for dashboard sharded runs."""

from __future__ import annotations

import sys
from pathlib import Path


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one match in {path}, found {count}")
    path.write_text(text.replace(old, new, 1))


def _replace_once_unless_present(
    path: Path, old: str, new: str, marker: str, *, allow_absent: bool = False
) -> None:
    text = path.read_text()
    if marker in text:
        return
    count = text.count(old)
    if count == 0 and allow_absent:
        return
    if count != 1:
        raise RuntimeError(f"expected one match in {path}, found {count}")
    path.write_text(text.replace(old, new, 1))


def patch_azure_py(root: Path) -> None:
    path = root / "src/elastic_blast/azure.py"
    _replace_once_unless_present(
        path,
        (
            "        # Deploy finalizer\n"
            "        if self.auto_shutdown:\n"
            "            self._submit_finalizer_job()\n"
        ),
        (
            "        # Deploy finalizer. In partitioned/sharded mode this is also the\n"
            "        # result-merger and terminal marker writer, not just an "
            "auto-shutdown hook.\n"
            "        self._submit_finalizer_job()\n"
        ),
        "result-merger and terminal marker writer",
        allow_absent=True,
    )
    _replace_once_unless_present(
        path,
        (
            "            'ELB_DB_PARTITIONS': str(cfg.blast.db_partitions) "
            "if cfg.blast.db_partitions > 0 else '0',\n"
            "            'ELB_BLAST_PROGRAM': cfg.blast.program,\n"
        ),
        (
            "            'ELB_DB_PARTITIONS': str(cfg.blast.db_partitions) "
            "if cfg.blast.db_partitions > 0 else '0',\n"
            "            'ELB_BLAST_PROGRAM': cfg.blast.program,\n"
            "            'ELB_BLAST_OPTIONS': cfg.blast.options,\n"
        ),
        "'ELB_BLAST_OPTIONS': cfg.blast.options",
    )
    _replace_once_unless_present(
        path,
        (
            "        subs = {\n"
            "            'ELB_DOCKER_IMAGE': cfg.azure.elb_docker_image,\n"
            "            'ELB_RESULTS': self._results_path(),\n"
        ),
        (
            "        subs = {\n"
            "            'ELB_DOCKER_IMAGE': cfg.azure.elb_docker_image,\n"
            "            'ELB_FINALIZER_DOCKER_IMAGE': cfg.azure.cjs_docker_image,\n"
            "            'ELB_RESULTS': self._results_path(),\n"
        ),
        "'ELB_FINALIZER_DOCKER_IMAGE': cfg.azure.cjs_docker_image",
    )


def patch_finalizer_template(root: Path) -> None:
    path = root / "src/elastic_blast/templates/elb-finalizer-aks.yaml.template"
    _replace_once_unless_present(
        path,
        "        image: ${ELB_DOCKER_IMAGE}\n",
        "        image: ${ELB_FINALIZER_DOCKER_IMAGE}\n",
        "image: ${ELB_FINALIZER_DOCKER_IMAGE}",
    )
    _replace_once_unless_present(
        path,
        (
            "        - name: ELB_BLAST_PROGRAM\n"
            '          value: "${ELB_BLAST_PROGRAM}"\n'
            "        - name: BLAST_ELB_JOB_ID\n"
        ),
        (
            "        - name: ELB_BLAST_PROGRAM\n"
            '          value: "${ELB_BLAST_PROGRAM}"\n'
            "        - name: ELB_BLAST_OPTIONS\n"
            '          value: "${ELB_BLAST_OPTIONS}"\n'
            "        - name: BLAST_ELB_JOB_ID\n"
        ),
        "name: ELB_BLAST_OPTIONS",
    )
    _replace_once_unless_present(
        path,
        "      restartPolicy: Never\n  # The finalizer writes terminal SUCCESS/FAILURE markers",
        (
            "      restartPolicy: Never\n"
            "      tolerations:\n"
            "      - key: workload\n"
            "        operator: Equal\n"
            "        value: blast\n"
            "        effect: NoSchedule\n"
            "      - key: CriticalAddonsOnly\n"
            "        operator: Exists\n"
            "        effect: NoSchedule\n"
            "  # The finalizer writes terminal SUCCESS/FAILURE markers"
        ),
        "key: CriticalAddonsOnly",
    )


def patch_finalizer_script(root: Path, merge_script_source: Path) -> None:
    path = root / "src/elastic_blast/templates/scripts/elb-finalizer-aks.sh"
    merge_script_target = path.parent / "merge-sharded-results.sh"
    merge_script_target.write_text(merge_script_source.read_text())

    _replace_once_unless_present(
        path,
        (
            'MARKER_DIR="${ELB_RESULTS}/${ELB_METADATA_DIR}"\n'
            "if azcopy login --identity >/dev/null 2>&1; then\n"
            '    if azcopy list "${MARKER_DIR}/SUCCESS.txt" '
            ">/dev/null 2>&1; then\n"
            '        if [ "${ELB_DB_PARTITIONS:-0}" -gt 0 ]; then\n'
            '            if azcopy list "${ELB_RESULTS}/merged_results.out.gz" '
            ">/dev/null 2>&1 && \\\n"
            '               azcopy list "${ELB_RESULTS}/merge-report.json" '
            ">/dev/null 2>&1; then\n"
            '                echo "SUCCESS.txt and merge artifacts already '
            'present; skipping finalizer"\n'
            "                exit 0\n"
            "            fi\n"
            '            echo "SUCCESS.txt already present but merge artifacts '
            'are missing; continuing merge"\n'
            "        else\n"
            '            echo "SUCCESS.txt already present at ${MARKER_DIR}; '
            'skipping finalizer"\n'
            "            exit 0\n"
            "        fi\n"
            "    fi\n"
            '    if azcopy list "${MARKER_DIR}/FAILURE.txt" '
            ">/dev/null 2>&1; then\n"
            '        echo "FAILURE.txt already present at ${MARKER_DIR}; '
            'skipping finalizer"\n'
            "        exit 0\n"
            "    fi\n"
            "fi\n"
        ),
        (
            'MARKER_DIR="${ELB_RESULTS}/${ELB_METADATA_DIR}"\n'
            "blob_exists() {\n"
            "    local output\n"
            '    output=$(azcopy list "$1" 2>/dev/null || true)\n'
            "    printf '%s\\n' \"$output\" | grep -Ev '^(INFO:|$)' >/dev/null\n"
            "}\n"
            "if azcopy login --identity >/dev/null 2>&1; then\n"
            '    if blob_exists "${MARKER_DIR}/SUCCESS.txt"; then\n'
            '        if [ "${ELB_DB_PARTITIONS:-0}" -gt 0 ]; then\n'
            '            if blob_exists "${ELB_RESULTS}/merged_results.out.gz" && \\\n'
            '               blob_exists "${ELB_RESULTS}/merge-report.json"; then\n'
            '                echo "SUCCESS.txt and merge artifacts already '
            'present; skipping finalizer"\n'
            "                exit 0\n"
            "            fi\n"
            '            echo "SUCCESS.txt already present but merge artifacts '
            'are missing; continuing merge"\n'
            "        else\n"
            '            echo "SUCCESS.txt already present at ${MARKER_DIR}; '
            'skipping finalizer"\n'
            "            exit 0\n"
            "        fi\n"
            "    fi\n"
            '    if blob_exists "${MARKER_DIR}/FAILURE.txt"; then\n'
            '        echo "FAILURE.txt already present at ${MARKER_DIR}; '
            'skipping finalizer"\n'
            "        exit 0\n"
            "    fi\n"
            "fi\n"
        ),
        "blob_exists()",
    )
    _replace_once_unless_present(
        path,
        (
            '            if ! azcopy cp "${SHARD_DIR}/*.out.gz" "$LOCAL_DIR/" '
            '--log-level=ERROR 2>/dev/null; then\n'
        ),
        (
            '            if ! azcopy cp "${SHARD_DIR}/*" "$LOCAL_DIR/" '
            '--include-pattern "*.out.gz" --log-level=ERROR 2>/dev/null; then\n'
        ),
        '--include-pattern "*.out.gz"',
    )
    _replace_once_unless_present(
        path,
        (
            '        TOTAL_ROWS=$(wc -l < "$MERGE_INPUT" 2>/dev/null || echo 0)\n'
            '        echo "Downloaded $SHARD_COUNT shard files, $TOTAL_ROWS tabular rows"\n\n'
            '        if ! /scripts/merge-sharded-results.sh \\\n'
        ),
        (
            '        TOTAL_ROWS=$(wc -l < "$MERGE_INPUT" 2>/dev/null || echo 0)\n'
            '        echo "Downloaded $SHARD_COUNT shard files, $TOTAL_ROWS tabular rows"\n\n'
            '        ORACLE_BLOB="${ELB_RESULTS}/${ELB_METADATA_DIR}/tie-order-oracle.txt"\n'
            '        ORACLE_URLS_BLOB="${ELB_RESULTS}/${ELB_METADATA_DIR}/'
            'tie-order-oracle-urls.txt"\n'
            '        ORACLE_STRICT_BLOB="${ELB_RESULTS}/${ELB_METADATA_DIR}/'
            'tie-order-oracle-strict.txt"\n'
            '        ORACLE_FILE="$MERGE_DIR/tie-order-oracle.txt"\n'
            '        if blob_exists "$ORACLE_BLOB"; then\n'
            '            if azcopy cp "$ORACLE_BLOB" "$ORACLE_FILE" '
            '--log-level=ERROR 2>/dev/null; then\n'
            '                export ELB_TIE_ORDER_FILE="$ORACLE_FILE"\n'
            '                echo "Using tie-order oracle from ${ORACLE_BLOB}"\n'
            '            else\n'
            '                echo "WARNING: tie-order oracle exists but could not be '
            'downloaded: ${ORACLE_BLOB}"\n'
            '            fi\n'
            '        fi\n\n'
            '        if [ -z "${ELB_TIE_ORDER_FILE:-}" ] && '
            'blob_exists "$ORACLE_URLS_BLOB"; then\n'
            '            ORACLE_URLS_FILE="$MERGE_DIR/tie-order-oracle-urls.txt"\n'
            '            ORACLE_PART_DIR="$MERGE_DIR/tie-order-oracle-parts"\n'
            '            mkdir -p "$ORACLE_PART_DIR"\n'
            '            if azcopy cp "$ORACLE_URLS_BLOB" "$ORACLE_URLS_FILE" '
            '--log-level=ERROR 2>/dev/null; then\n'
            '                idx=0\n'
            '                while IFS= read -r part_url; do\n'
            '                    [ -z "$part_url" ] && continue\n'
            '                    part_file=$(printf "%s/part-%06d.txt" '
            '"$ORACLE_PART_DIR" "$idx")\n'
            '                    if ! azcopy cp "$part_url" "$part_file" '
            '--log-level=ERROR 2>/dev/null; then\n'
            '                        echo "WARNING: tie-order oracle part could not '
            'be downloaded: ${part_url}"\n'
            '                        rm -f "$part_file"\n'
            '                    fi\n'
            '                    idx=$((idx + 1))\n'
            '                done < "$ORACLE_URLS_FILE"\n'
            '                if find "$ORACLE_PART_DIR" -type f '
            '-name "part-*.txt" | grep -q .; then\n'
            '                    find "$ORACLE_PART_DIR" -type f '
            '-name "part-*.txt" | sort | xargs cat > "$ORACLE_FILE"\n'
            '                    export ELB_TIE_ORDER_FILE="$ORACLE_FILE"\n'
            '                    echo "Using DB-order tie oracle parts from ${ORACLE_URLS_BLOB}"\n'
            '                fi\n'
            '            fi\n'
            '        fi\n'
            '        if [ -n "${ELB_TIE_ORDER_FILE:-}" ] && '
            'blob_exists "$ORACLE_STRICT_BLOB"; then\n'
            '            export ELB_TIE_ORDER_STRICT="1"\n'
            '        fi\n\n'
            '        if ! /scripts/merge-sharded-results.sh \\\n'
        ),
        "ELB_TIE_ORDER_FILE",
    )

    text = path.read_text()
    if (
        "MERGE_OUTFMT=$(python3" in text
        and '"$MERGE_INPUT" "$MERGE_OUTPUT" "$MERGE_REPORT"' in text
    ):
        return
    if '"$MAX_HITS" "$MERGE_INPUT" "$MERGE_OUTPUT"' in text:
        raise RuntimeError(
            "elastic-blast-azure finalizer has the legacy tabular merge patch; "
            "update the cloned runtime to the XML-aware finalizer before building"
        )

    raise RuntimeError(
        "elastic-blast-azure finalizer is not XML-aware; update the cloned runtime "
        "before building the dashboard terminal image"
    )


def patch_init_shard_script(root: Path) -> None:
    path = root / "src/elastic_blast/templates/scripts/init-db-shard-aks.sh"
    _replace_once_unless_present(
        path,
        (
            "VOLUMES=$(cat /tmp/manifest.txt)\n"
            'echo "Volumes: ${VOLUMES}"\n\n'
            "# Step 2: Derive base DB URL (strip shard part from prefix)\n"
        ),
        (
            "VOLUMES=$(cat /tmp/manifest.txt)\n"
            'echo "Volumes: ${VOLUMES}"\n\n'
            "write_volpaths() {\n"
            '    local volpaths=""\n'
            '    for vol in $VOLUMES; do\n'
            '        [ -n "$volpaths" ] && volpaths="$volpaths "\n'
            '        volpaths="${volpaths}$(pwd)/${vol}"\n'
            "    done\n"
            '    echo "VOLPATHS=${volpaths}" > /tmp/shard_volpaths.txt\n'
            '    echo "Volume paths: ${volpaths}"\n'
            "}\n\n"
            'if [ -s .download-complete ]; then\n'
            '    missing=0\n'
            '    for vol in $VOLUMES; do\n'
            '        if ! find . -maxdepth 1 -name "${vol}.*" | grep -q .; then\n'
            '            missing=1\n'
            '            echo "Warm-cache marker found but ${vol} files are missing"\n'
            "        fi\n"
            "    done\n"
            '    if [ "$missing" = "0" ]; then\n'
            '        echo "DOWNLOAD_SKIP existing shard=${ELB_SHARD_IDX}"\n'
            "        write_volpaths\n"
            "        exit 0\n"
            "    fi\n"
            "fi\n\n"
            "# Step 2: Derive base DB URL (strip shard part from prefix)\n"
        ),
        "DOWNLOAD_SKIP existing shard=${ELB_SHARD_IDX}",
    )
    _replace_once_unless_present(
        path,
        (
            'echo "Total size: $(du -sh . 2>/dev/null | cut -f1)"\n\n'
            "# Build volume path string for BLAST -db (space-separated, bypasses .nal)\n"
            'VOLPATHS=""\n'
            "for VOL in $VOLUMES; do\n"
            '    [ -n "$VOLPATHS" ] && VOLPATHS="$VOLPATHS "\n'
            '    VOLPATHS="${VOLPATHS}$(pwd)/${VOL}"\n'
            "done\n"
            'echo "VOLPATHS=${VOLPATHS}" > /tmp/shard_volpaths.txt\n'
            'echo "Volume paths: ${VOLPATHS}"\n'
        ),
        (
            'echo "Total size: $(du -sh . 2>/dev/null | cut -f1)"\n'
            "touch .download-complete\n\n"
            "# Build volume path string for BLAST -db (space-separated, bypasses .nal)\n"
            "write_volpaths\n"
        ),
        "touch .download-complete",
    )


def patch_aks_workload_tolerations(root: Path) -> None:
    templates = {
        "blast-batch-job-aks.yaml.template": "OnFailure",
        "blast-batch-job-shard-ssd-aks.yaml.template": "OnFailure",
        "job-init-pv-aks.yaml.template": "Never",
        "job-init-pv-partitioned-aks.yaml.template": "Never",
        "job-init-local-ssd-aks.yaml.template": "Never",
        "job-init-ssd-shard-aks.yaml.template": "Never",
        "job-submit-jobs-aks.yaml.template": "Never",
        "vmtouch-daemonset-aks.yaml.template": "Always",
    }
    tolerations = """      tolerations:
      - key: workload
        operator: Equal
        value: blast
        effect: NoSchedule
"""
    node_selector = """      nodeSelector:
        workload: blast
"""
    for name, restart_policy in templates.items():
        path = root / "src/elastic_blast/templates" / name
        text = path.read_text()
        if "key: workload" not in text:
            old = f"      restartPolicy: {restart_policy}\n"
            new = f"      restartPolicy: {restart_policy}\n{tolerations}"
            _replace_once(path, old, new)
            text = path.read_text()
        if "nodeSelector:\n        workload: blast" not in text:
            insert_at = text.index(tolerations) + len(tolerations)
            text = text[:insert_at] + node_selector + text[insert_at:]
            path.write_text(text)


def main() -> int:
    if len(sys.argv) not in {2, 3}:
        print(
            "usage: patch_elastic_blast.py /path/to/elastic-blast-azure [merge-script]",
            file=sys.stderr,
        )
        return 2
    root = Path(sys.argv[1]).resolve()
    merge_script_source = (
        Path(sys.argv[2]).resolve()
        if len(sys.argv) == 3
        else Path(__file__).with_name("merge-sharded-results.sh")
    )
    if not (root / "src/elastic_blast").is_dir():
        print(f"not an elastic-blast-azure source tree: {root}", file=sys.stderr)
        return 2
    if not merge_script_source.is_file():
        print(f"merge script not found: {merge_script_source}", file=sys.stderr)
        return 2

    patch_azure_py(root)
    patch_finalizer_template(root)
    patch_finalizer_script(root, merge_script_source)
    patch_init_shard_script(root)
    patch_aks_workload_tolerations(root)
    print("patched elastic-blast-azure finalizer for sharded result merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
