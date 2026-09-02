#!/usr/bin/env bash
# Prove same-snapshot full-DB equivalence for DB-order exact shard merging.

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
IMAGE=${ELB_EXACT_VERIFY_IMAGE:-ncbi/blast:2.17.0}
TMP_DIR=$(mktemp -d /tmp/elb-exact-sharding.XXXXXX)

cleanup() {
    if [[ "${KEEP_TMP:-0}" != "1" ]]; then
        rm -rf "$TMP_DIR"
    fi
}
trap cleanup EXIT

mkdir -p "$TMP_DIR/shard_00" "$TMP_DIR/shard_01" "$TMP_DIR/shard_02"
python3 - "$TMP_DIR" <<'PY'
import random
import sys
from pathlib import Path

root = Path(sys.argv[1])
rng = random.Random(20260902)
query = "".join(rng.choice("ACGT") for _ in range(180))
(root / "query.fa").write_text(f">query1\n{query}\n")
records = []
for index in range(45):
    sequence = list(query)
    mutations = 0 if index < 25 else 1 if index < 38 else 2
    for offset in range(mutations):
        position = (19 * index + 31 * offset) % len(sequence)
        sequence[position] = {"A": "C", "C": "G", "G": "T", "T": "A"}[
            sequence[position]
        ]
    records.append((f"s{index:02d}", "".join(sequence)))


def write_fasta(path: Path, subset: list[tuple[str, str]]) -> None:
    path.write_text("".join(f">{name}\n{sequence}\n" for name, sequence in subset))


write_fasta(root / "full.fa", records)
for shard in range(3):
    write_fasta(root / f"shard{shard}.fa", records[shard * 15 : (shard + 1) * 15])
PY
chmod -R a+rwX "$TMP_DIR"

docker run --rm --entrypoint /bin/bash \
    -v "$TMP_DIR:/work" \
    -v "$ROOT:/repo:ro" \
    "$IMAGE" -lc '
set -euo pipefail
makeblastdb -in /work/full.fa -dbtype nucl -parse_seqids -out /work/full >/dev/null
blastdbcmd -db /work/full -entry all -outfmt "%a" > /work/full.oracle
blastn -query /work/query.fa -db /work/full -task blastn -dust no \
    -evalue 100 -searchsp 999999 -max_target_seqs 30 -outfmt 5 -out /work/full.xml
blastn -query /work/query.fa -db /work/full -task blastn -dust no \
    -evalue 100 -searchsp 999999 -max_target_seqs 30 \
    -outfmt "6 std score" -out /work/full.tsv
for shard in 0 1 2; do
    makeblastdb -in "/work/shard${shard}.fa" -dbtype nucl -parse_seqids \
        -out "/work/shard${shard}" >/dev/null
    blastdbcmd -db "/work/shard${shard}" -entry all -outfmt "%a" \
        > "/work/shard${shard}.oracle"
    blastn -query /work/query.fa -db "/work/shard${shard}" -task blastn -dust no \
        -evalue 100 -searchsp 999999 -max_target_seqs 30 -outfmt 5 \
        -out "/work/shard_0${shard}/result.out"
    gzip -f "/work/shard_0${shard}/result.out"
    blastn -query /work/query.fa -db "/work/shard${shard}" -task blastn -dust no \
        -evalue 100 -searchsp 999999 -max_target_seqs 30 \
        -outfmt "6 std score" -out "/work/shard${shard}.tsv"
done
cat /work/shard0.oracle /work/shard1.oracle /work/shard2.oracle > /work/concat.oracle
cmp /work/full.oracle /work/concat.oracle
cat /work/shard0.tsv /work/shard1.tsv /work/shard2.tsv > /work/all.tsv
ELB_TIE_ORDER_FILE=/work/concat.oracle ELB_TIE_ORDER_SOURCE=db_order \
    bash /repo/terminal/merge-sharded-results.sh \
    /work/all.tsv /work/merged.tsv.gz /work/tab-report.json 3 blastn \
    "-task blastn -dust no -evalue 100 -searchsp 999999 -outfmt 6 std score -max_target_seqs 30"
: > /work/xml-input.tsv
ELB_TIE_ORDER_FILE=/work/concat.oracle ELB_TIE_ORDER_SOURCE=db_order \
    bash /repo/terminal/merge-sharded-results.sh \
    /work/xml-input.tsv /work/merged.xml.gz /work/xml-report.json 3 blastn \
    "-task blastn -dust no -evalue 100 -searchsp 999999 -outfmt 5 -max_target_seqs 30"
'

uv run python "$ROOT/scripts/dev/compare-blast-xml.py" \
    --left "$TMP_DIR/full.xml" \
    --right "$TMP_DIR/merged.xml.gz" \
    --json "$TMP_DIR/xml-compare.json" >/dev/null

python3 - "$TMP_DIR" <<'PY'
import gzip
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
full_rows = [line for line in (root / "full.tsv").read_text().splitlines() if line]
with gzip.open(root / "merged.tsv.gz", "rt") as handle:
    merged_rows = [
        line.rstrip("\n")
        for line in handle
        if line.strip() and not line.startswith("#")
    ]
xml_compare = json.loads((root / "xml-compare.json").read_text())
tab_report = json.loads((root / "tab-report.json").read_text())
xml_report = json.loads((root / "xml-report.json").read_text())
result = {
    "oracle_concat_exact": (root / "full.oracle").read_bytes()
    == (root / "concat.oracle").read_bytes(),
    "tabular_exact": full_rows == merged_rows,
    "xml_exact": xml_compare["equivalent"],
    "xml_difference_count": xml_compare["difference_count"],
    "subjects": tab_report["total_output_subjects"],
    "tabular_selection": tab_report["selection_equivalence"],
    "xml_selection": xml_report["selection_equivalence"],
    "xml_statistics": xml_report["statistics_equivalence"],
    "ranking_basis": tab_report["ranking_basis"],
}
print(json.dumps(result, indent=2, sort_keys=True))
if not all(
    (
        result["oracle_concat_exact"],
        result["tabular_exact"],
        result["xml_exact"],
        result["xml_difference_count"] == 0,
        result["tabular_selection"] == "full_db_hitlist_exact",
        result["xml_selection"] == "full_db_hitlist_exact",
        result["xml_statistics"] == "full_db_exact",
    )
):
    raise SystemExit(1)
PY

if [[ "${KEEP_TMP:-0}" == "1" ]]; then
    echo "Kept evidence: $TMP_DIR"
fi
