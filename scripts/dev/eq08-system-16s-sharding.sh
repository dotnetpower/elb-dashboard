#!/usr/bin/env bash
set -euo pipefail

stamp=$(date -u +%Y%m%dT%H%M%SZ)
out_dir="/workspace/evidence/eq08-system-16s-sharding-${stamp}"
work_dir="/workspace/eq08-16s-work-${stamp}"
db_root="/workspace/eq08-16s-db-${stamp}"
mkdir -p "$out_dir/results" "$work_dir/db" "$db_root"

storage_account=${EQ08_STORAGE_ACCOUNT:-elbstg01}
container=${EQ08_CONTAINER:-blast-db}
source_db=${EQ08_SOURCE_DB:-16S_ribosomal_RNA}
query_accession=${EQ08_QUERY_ACCESSION:-NR_025211.1}
num_shards=${EQ08_NUM_SHARDS:-4}
searchsp=${EQ08_SEARCHSP:-57425628120}
namespace=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace 2>/dev/null || echo elb-equivalence)
merge_configmap=${EQ08_MERGE_CONFIGMAP:-eq08-merge-tools}
export AZCOPY_AUTO_LOGIN_TYPE=${AZCOPY_AUTO_LOGIN_TYPE:-MSI}

printf 'EQ08_SYSTEM_16S_STARTED=%s\n' "$stamp" | tee "$out_dir/summary.env"
printf 'NODE_NAME=%s\n' "${NODE_NAME:-unknown}" | tee -a "$out_dir/summary.env"
printf 'STORAGE_ACCOUNT=%s\n' "$storage_account" | tee -a "$out_dir/summary.env"
printf 'SOURCE_DB=%s\n' "$source_db" | tee -a "$out_dir/summary.env"
printf 'QUERY_ACCESSION=%s\n' "$query_accession" | tee -a "$out_dir/summary.env"
printf 'NUM_SHARDS=%s\n' "$num_shards" | tee -a "$out_dir/summary.env"
printf 'SEARCHSP=%s\n' "$searchsp" | tee -a "$out_dir/summary.env"
blastn -version | tee "$out_dir/results/blastn-version.txt"
makeblastdb -version | tee "$out_dir/results/makeblastdb-version.txt"

merge_script="$work_dir/merge-sharded-results.sh"
kubectl -n "$namespace" get configmap "$merge_configmap" -o jsonpath='{.data.merge-sharded-results\.sh}' > "$merge_script"
chmod +x "$merge_script"

db_dest="$db_root/$source_db"
mkdir -p "$db_dest"
echo "DOWNLOAD_BEGIN $source_db" | tee -a "$out_dir/summary.env"
azcopy cp "https://${storage_account}.blob.core.windows.net/${container}/${source_db}/" "$db_dest/" --recursive=true --log-level=ERROR
find "$db_dest" -maxdepth 3 -type f -printf '%P\t%s\n' | sort > "$out_dir/results/${source_db}-files.txt"
echo "DOWNLOAD_END $source_db" | tee -a "$out_dir/summary.env"

db_path="$db_root/$source_db/$source_db/$source_db"
blastdbcmd -db "$db_path" -info | tee "$out_dir/results/full-db-info.txt"
blastdbcmd -db "$db_path" -entry "$query_accession" -out "$work_dir/query.fa"
cp "$work_dir/query.fa" "$out_dir/results/query.fa"
blastdbcmd -db "$db_path" -entry all -out "$work_dir/full.fa"
blastdbcmd -db "$db_path" -entry all -outfmt '%a' > "$work_dir/full-accession-order.txt"
cp "$work_dir/full-accession-order.txt" "$out_dir/results/full-accession-order.txt"

python3 - <<'PY' "$work_dir/full.fa" "$work_dir" "$num_shards" "$out_dir/results/split-summary.json"
import json
import math
import pathlib
import sys

fasta_path = pathlib.Path(sys.argv[1])
work_dir = pathlib.Path(sys.argv[2])
num_shards = int(sys.argv[3])
summary_path = pathlib.Path(sys.argv[4])
records = []
header = None
sequence = []
for line in fasta_path.read_text(encoding='utf-8').splitlines():
    if line.startswith('>'):
        if header is not None:
            records.append((header, ''.join(sequence)))
        header = line
        sequence = []
    else:
        sequence.append(line.strip())
if header is not None:
    records.append((header, ''.join(sequence)))
chunk_size = math.ceil(len(records) / num_shards)
shards = []
for shard in range(num_shards):
    shard_records = records[shard * chunk_size:(shard + 1) * chunk_size]
    shard_path = work_dir / f'shard_{shard:02d}.fa'
    with shard_path.open('w', encoding='utf-8') as handle:
        for rec_header, rec_sequence in shard_records:
            handle.write(rec_header + '\n')
            for idx in range(0, len(rec_sequence), 80):
                handle.write(rec_sequence[idx:idx + 80] + '\n')
    shards.append({'shard': shard, 'records': len(shard_records)})
summary_path.write_text(json.dumps({'records': len(records), 'num_shards': num_shards, 'chunk_size': chunk_size, 'shards': shards}, indent=2) + '\n')
PY

full_xml="$out_dir/results/full.xml"
full_opts="-evalue 10 -max_target_seqs 500 -outfmt 5 -word_size 28 -dust yes -searchsp ${searchsp}"
shard_opts="-evalue 10 -max_target_seqs 5000 -outfmt 5 -word_size 28 -dust yes -searchsp ${searchsp}"
merge_opts="-evalue 10 -max_target_seqs 500 -outfmt 5 -word_size 28 -dust yes -searchsp ${searchsp}"

echo "FULL_RUN_BEGIN" | tee -a "$out_dir/summary.env"
blastn -query "$work_dir/query.fa" -db "$db_path" $full_opts -out "$full_xml"
echo "FULL_RUN_END" | tee -a "$out_dir/summary.env"

for shard in $(seq 0 $((num_shards - 1))); do
  shard_id=$(printf '%02d' "$shard")
  shard_dir="$work_dir/shard_${shard_id}"
  mkdir -p "$shard_dir" "$out_dir/results/shard_${shard_id}"
  echo "SHARD_BUILD_BEGIN $shard_id" | tee -a "$out_dir/summary.env"
  makeblastdb -in "$work_dir/shard_${shard_id}.fa" -dbtype nucl -parse_seqids -out "$work_dir/db/${source_db}_shard_${shard_id}" > "$out_dir/results/shard_${shard_id}/makeblastdb.log" 2>&1
  blastdbcmd -db "$work_dir/db/${source_db}_shard_${shard_id}" -info > "$out_dir/results/shard_${shard_id}/db-info.txt"
  echo "SHARD_RUN_BEGIN $shard_id" | tee -a "$out_dir/summary.env"
  blastn -query "$work_dir/query.fa" -db "$work_dir/db/${source_db}_shard_${shard_id}" $shard_opts -out "$shard_dir/batch.out"
  gzip -c "$shard_dir/batch.out" > "$shard_dir/batch.out.gz"
  cp "$shard_dir/batch.out.gz" "$out_dir/results/shard_${shard_id}/batch.out.gz"
  echo "SHARD_RUN_END $shard_id" | tee -a "$out_dir/summary.env"
done

input_tsv="$work_dir/merge-input.tsv"
: > "$input_tsv"

echo "MERGE_DEFAULT_BEGIN" | tee -a "$out_dir/summary.env"
default_xml_gz="$out_dir/results/merged-default.xml.gz"
default_report="$out_dir/results/merge-default-report.json"
"$merge_script" "$input_tsv" "$default_xml_gz" "$default_report" "$num_shards" blastn "$merge_opts" 2> "$out_dir/results/merge-default.stderr"
echo "MERGE_DEFAULT_END" | tee -a "$out_dir/summary.env"

echo "MERGE_ORACLE_BEGIN" | tee -a "$out_dir/summary.env"
oracle_xml_gz="$out_dir/results/merged-oracle.xml.gz"
oracle_report="$out_dir/results/merge-oracle-report.json"
ELB_TIE_ORDER_FILE="$work_dir/full-accession-order.txt" "$merge_script" "$input_tsv" "$oracle_xml_gz" "$oracle_report" "$num_shards" blastn "$merge_opts" 2> "$out_dir/results/merge-oracle.stderr"
echo "MERGE_ORACLE_END" | tee -a "$out_dir/summary.env"

python3 - <<'PY' "$full_xml" "$default_xml_gz" "$oracle_xml_gz" "$default_report" "$oracle_report" "$out_dir/summary.json"
import gzip
import json
import pathlib
import sys
import xml.etree.ElementTree as ET

full_xml, default_gz, oracle_gz, default_report, oracle_report, out_json = map(pathlib.Path, sys.argv[1:])

def parse(path):
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rb') as handle:
        return ET.parse(handle).getroot()

def text(node, name):
    found = node.find(name) if node is not None else None
    return found.text if found is not None else None

def summarize(root):
    hits = root.findall('.//Hit')
    hsps = root.findall('.//Hsp')
    stats = root.find('.//Statistics')
    accessions = [text(hit, 'Hit_accession') for hit in hits]
    top = hits[0] if hits else None
    return {
        'hit_count': len(hits),
        'hsp_count': len(hsps),
        'top_hit_accession': text(top, 'Hit_accession'),
        'statistics_db_len': text(stats, 'Statistics_db-len'),
        'statistics_db_num': text(stats, 'Statistics_db-num'),
        'statistics_eff_space': text(stats, 'Statistics_eff-space'),
        'statistics_hsp_len': text(stats, 'Statistics_hsp-len'),
        'accessions': accessions,
    }

def compare(full, merged):
    first_mismatch = None
    for idx, (left, right) in enumerate(zip(full['accessions'], merged['accessions']), start=1):
        if left != right:
            first_mismatch = {'rank': idx, 'full': left, 'merged': right}
            break
    return {
        'stats_equal': all(full[key] == merged[key] for key in ['statistics_db_len', 'statistics_db_num', 'statistics_eff_space', 'statistics_hsp_len']),
        'top_accession_equal': full['top_hit_accession'] == merged['top_hit_accession'],
        'top10_equal': full['accessions'][:10] == merged['accessions'][:10],
        'ordered_accessions_equal': full['accessions'] == merged['accessions'],
        'accession_overlap': len(set(full['accessions']) & set(merged['accessions'])),
        'full_hit_count': full['hit_count'],
        'merged_hit_count': merged['hit_count'],
        'first_mismatch': first_mismatch,
    }

full = summarize(parse(full_xml))
default = summarize(parse(default_gz))
oracle = summarize(parse(oracle_gz))
payload = {
    'full': {key: value for key, value in full.items() if key != 'accessions'},
    'default_merge': {
        'summary': {key: value for key, value in default.items() if key != 'accessions'},
        'comparison': compare(full, default),
        'merge_report': json.loads(default_report.read_text(encoding='utf-8')),
    },
    'oracle_merge': {
        'summary': {key: value for key, value in oracle.items() if key != 'accessions'},
        'comparison': compare(full, oracle),
        'merge_report': json.loads(oracle_report.read_text(encoding='utf-8')),
    },
}
out_json.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
print(json.dumps(payload, indent=2))
PY

printf 'EQ08_SYSTEM_16S_FINISHED=%s\n' "$(date -u +%Y%m%dT%H%M%SZ)" | tee -a "$out_dir/summary.env"
echo "Evidence: $out_dir"