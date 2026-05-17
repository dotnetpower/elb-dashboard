#!/usr/bin/env bash
# Verify local outfmt 5 sharded XML merge against a real BLAST+ run.

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
IMAGE=${ELB_TERMINAL_IMAGE:-elb-terminal:dev}
SEARCHSP=${ELB_LOCAL_SEARCHSP:-4096}
TMP_DIR=$(mktemp -d /tmp/elb-blast-xml-searchsp.XXXXXX)

cleanup() {
    if [[ "${KEEP_TMP:-0}" != "1" ]]; then
        rm -rf "$TMP_DIR"
    fi
}
trap cleanup EXIT

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "ERROR: Docker image not found: $IMAGE" >&2
    echo "Build/start the local terminal sidecar first, then rerun this script." >&2
    exit 2
fi

chmod 777 "$TMP_DIR"
mkdir -p "$TMP_DIR/db" "$TMP_DIR/shard_00" "$TMP_DIR/shard_01"
chmod -R 777 "$TMP_DIR"

printf '%s\n' \
    '>query1' \
    'ACGTACGTACGTACGTACGTACGTACGTACGT' \
    > "$TMP_DIR/query.fa"

printf '%s\n' \
    '>subject_best' \
    'ACGTACGTACGTACGTACGTACGTACGTACGT' \
    '>subject_slow' \
    'ACGTACGTACGTACGTACGTACGTACGTTCGA' \
    '>subject_bit' \
    'ACGTACGTACGTACGTACGTACGTACGTACGA' \
    '>subject_far' \
    'TTTTACGTACGTACGTACGTACGTACGTTTTT' \
    > "$TMP_DIR/full.fa"

printf '%s\n' \
    '>subject_best' \
    'ACGTACGTACGTACGTACGTACGTACGTACGT' \
    '>subject_slow' \
    'ACGTACGTACGTACGTACGTACGTACGTTCGA' \
    > "$TMP_DIR/shard_00.fa"

printf '%s\n' \
    '>subject_bit' \
    'ACGTACGTACGTACGTACGTACGTACGTACGA' \
    '>subject_far' \
    'TTTTACGTACGTACGTACGTACGTACGTTTTT' \
    > "$TMP_DIR/shard_01.fa"

chmod -R 777 "$TMP_DIR"

docker run --rm --entrypoint /bin/bash -v "$TMP_DIR:/work" "$IMAGE" -lc "
set -euo pipefail
makeblastdb -in /work/full.fa -dbtype nucl -parse_seqids -out /work/db/full >/work/makeblastdb-full.log 2>&1
makeblastdb -in /work/shard_00.fa -dbtype nucl -parse_seqids -out /work/db/shard_00 >/work/makeblastdb-shard00.log 2>&1
makeblastdb -in /work/shard_01.fa -dbtype nucl -parse_seqids -out /work/db/shard_01 >/work/makeblastdb-shard01.log 2>&1
opts='-task blastn-short -dust no -evalue 1000 -searchsp $SEARCHSP -max_target_seqs 2 -outfmt 5'
blastn -query /work/query.fa -db /work/db/full \$opts -out /work/full.xml
shard_opts='-task blastn-short -dust no -evalue 1000 -searchsp $SEARCHSP -max_target_seqs 10 -outfmt 5'
blastn -query /work/query.fa -db /work/db/shard_00 \$shard_opts -out /work/shard_00/batch.out
blastn -query /work/query.fa -db /work/db/shard_01 \$shard_opts -out /work/shard_01/batch.out
gzip -c /work/shard_00/batch.out > /work/shard_00/batch.out.gz
gzip -c /work/shard_01/batch.out > /work/shard_01/batch.out.gz
"

: > "$TMP_DIR/all_hits.tsv"
/bin/bash "$ROOT/terminal/merge-sharded-results.sh" \
    "$TMP_DIR/all_hits.tsv" \
    "$TMP_DIR/merged.out.gz" \
    "$TMP_DIR/merge-report.json" \
    2 \
    blastn \
    "-task blastn-short -dust no -evalue 1000 -searchsp $SEARCHSP -max_target_seqs 2 -outfmt 5" \
    >/tmp/elb-local-merge-searchsp.stdout \
    2>/tmp/elb-local-merge-searchsp.stderr

python3 - "$TMP_DIR" <<'PY'
import gzip
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

root = Path(sys.argv[1])
full = ET.parse(root / "full.xml").getroot()
with gzip.open(root / "merged.out.gz", "rt") as handle:
    merged = ET.parse(handle).getroot()


def hits(xml_root):
    return [node.text for node in xml_root.findall(".//Hit_id")]


def hsp_tuples(xml_root):
    values = []
    for hit in xml_root.findall(".//Hit"):
        values.append(
            (
                hit.findtext("Hit_id"),
                hit.findtext("./Hit_hsps/Hsp/Hsp_evalue"),
                hit.findtext("./Hit_hsps/Hsp/Hsp_bit-score"),
            )
        )
    return values

report = json.loads((root / "merge-report.json").read_text())
full_hits = hits(full)
merged_hits = hits(merged)
full_hsps = hsp_tuples(full)
merged_hsps = hsp_tuples(merged)
result = {
    "tmp_dir": str(root),
    "full_hit_order": full_hits,
    "merged_hit_order": merged_hits,
    "order_equal": full_hits == merged_hits,
    "full_hsps": full_hsps,
    "merged_hsps": merged_hsps,
    "hsp_tuple_equal": full_hsps == merged_hsps,
    "merge_report": {
        key: report[key]
        for key in [
            "outfmt",
            "format",
            "queries",
            "total_input_hits",
            "total_output_hits",
            "ranking_basis",
        ]
    },
}
print(json.dumps(result, indent=2, sort_keys=True))
if not result["order_equal"] or not result["hsp_tuple_equal"]:
    raise SystemExit(1)
PY

cat /tmp/elb-local-merge-searchsp.stderr
if [[ "${KEEP_TMP:-0}" == "1" ]]; then
    echo "Kept temp directory: $TMP_DIR"
fi
