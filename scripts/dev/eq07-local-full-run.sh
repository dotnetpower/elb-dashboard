#!/usr/bin/env bash
set -euo pipefail

stamp=$(date -u +%Y%m%dT%H%M%SZ)
out_dir="/workspace/evidence/eq07-local-full-run-${stamp}"
db_root="/workspace/eq07-dbs-${stamp}"
mkdir -p "$out_dir/queries" "$out_dir/results" "$db_root"

storage_account=${EQ07_STORAGE_ACCOUNT:-elbstg01}
container=${EQ07_CONTAINER:-blast-db}
export AZCOPY_AUTO_LOGIN_TYPE=${AZCOPY_AUTO_LOGIN_TYPE:-MSI}

printf 'EQ07_LOCAL_FULL_STARTED=%s\n' "$stamp" | tee "$out_dir/summary.env"
printf 'STORAGE_ACCOUNT=%s\n' "$storage_account" | tee -a "$out_dir/summary.env"
printf 'CONTAINER=%s\n' "$container" | tee -a "$out_dir/summary.env"
blastn -version | tee "$out_dir/blastn-version.txt"

cat > "$out_dir/queries/16s.fa" <<'FASTA'
>NR_025211.1_rep0 Carnobacterium pleistocenium strain
GACGAACGCTGGCGGCATGCCTAATACATGCAAGTCGAACGCTTTGACTTCACCGGGTGCTTGCACCCAC
CGAAGTCAAGGAGTGGCGGACGGGTGAGTAACACGTGGGTAACCTGCCCATAAGAGGGGGATAACATTCG
GAAACGGATGCTAATACCGCATATTTCTAAACGTCACATGACGAATAGAAGAAAGGTGGCTTCGGCTACC
GCTTATGGATGGACCCGCGGCGTATTAGCTAGTTGGTGAGGTAATGGCTCACCAAGGCGATGATACGTAG
CCGACCTGAGAGGGTGATCGGCCACACTGGGACTGAGACACGGCCCAGACTCCTACGGGAGGCAGCAGTA
GGGAATCTTCCGCAATGGACGAAAGTCTGACGGAGCAATGCCGCGTGAGTGAAGAAGGTTTTCGGATCGT
AAAACTCTGTTGTTAGAGAAGAACAAGGATGAGAGTAACTGCTCATCCCCTGACGGTATCTAACCAGAAA
GCCACGGCTAACTACGTGCCAGCAGCCGCGGTAATACGTAGGTGGCAAGCGTTGTCCGGATTTATTGGGC
GTAAAGCGAGCGCAGGCGGTTCTTTAAGTCTGATGTGAAAGCCCCCGGCTCAACCGGGGAAGGTCATTGG
AAACTGGGGAACTTGAGTGCAGAAGAGGAGAGTGGAATTCCACGTGTAGCGGTGAAATGCGTAGATATGT
GGAGGAACACCAGTGGCGAAGGCGACTCTCTGGTCTGTAACTGACGCTGAGGCTCGAAAGCGTGGGGAGC
AAACAGGATTAGATACCCTGGTAGTCCACGCCGTAAACGATGAGTGCTAAGTGTTGGAGGGTTTCCGCCC
TTCAGTGCTGCAGCTAACGCATTAAGCACTCCGCCTGGGGAGTACGACCGCAAGGTTGAAACTCAAAGGA
ATTGACGGGGACCCGCACAAGCGGTGGAGCATGTGGTTTAATTCGAAGCAACGCGAAGAACCTTACCAGG
TCTTGACATCCTTTGACAACCCTAGAGATAGGGCTTTCCCTTCGGGGACAAAGTGACAGGTGGTGCATGG
TTGTCGTCAGCTCGTGTCGTGAGATGTTGGGTTAAGTCCCGCAACGAGCGCAACCCCTATTATTAGTTGC
CAGCATTCAGTTGGGCACTCTAGTGAGACTGCCGGTGATAAACCGGAGGAAGGTGGGGATGACGTCAAAT
CATCATGCCCCTTATGACCTGGGCTACACACGTGCTACAATGGATGGTACAACGAGTCGCAAAGTCGCGA
GGCTAAGCTAATCTCTTAAAGCCATTCTCAGTTCGGATTGTAGGCTGCAACTCGCCTGCATGAAGCCGGA
ATCGCTAGTAATCGCGGATCAGCACGCCGCGGTGAATACGTTCCCGGGTCTTGTACACACCGCCCGTCAC
ACCACGAGAGTTTGTAACACCCGAAGTCGGTGAGGTAACCCTTTTGGGAGCCAGCCGCCTAAGGTGGGAC
AGATAATTGGGGTGAA
FASTA

cat > "$out_dir/queries/18s.fa" <<'FASTA'
>NR_132222.1 Saccharomyces cerevisiae S288C 18S ribosomal RNA (RDN18-2), rRNA first_300bp
TATCTGGTTGATCCTGCCAGTAGTCATATGCTTGTCTCAAAGATTAAGCCATGCATGTCTAAGTATAAGCAATTTATACA
GTGAAACTGCGAATGGCTCATTAAATCAGTTATCGTTTATTTGATAGTTCCTTTACTACATGGTATAACTGTGGTAATTC
TAGAGCTAATACATGCTTAAAATCTCGACCCTTTGGAAGAGATGTATTTATTAGATAAAAAATCAATGTCTTCGGACTCT
TTGATGATTCATAATAACTTTTCGAATCGCATGGCCTTGTGCTGGCGATGGTTCATTCAA
FASTA

cat > "$out_dir/queries/its.fa" <<'FASTA'
>PZ364409.1 Saccharomyces cerevisiae isolate 30 internal transcribed spacer 1, partial sequence; 5.8S ribosomal RNA gene, complete sequence; and internal transcribed spacer 2, partial sequence
GGCAAGAGCATGAGAGCTTTTACTGGGCAAGAAGACAAGAGATGGAGAGTCCAGCCGGGCCTGCGCTTAA
GTGCGCCGTCTTGCTAGGCTTGTAAGTTTCTTTCTTGCTATTCCAAACGGTGAGAGATTTCTGTGCTTTT
GTTATAGGACAATTAAAACCGTTTCAATACAACACACTGTGGAGTTTTCATATCTTTGCAACTTTTTCTT
TGGGCATTCGAGCAATCGGGGCCCAGAGGTAACAAACACAAACAATTTTATCTATTCATTAAATTTTTGT
CAAAAACAAGAATTTTCGTAACTGGAAATTTTAAAATATTAAAAACTTTCAACAACGGATCTCTTGGTTC
TCGCATCGATGAAGAACGCAGCGAAATGCGATACGTAATGTGAATTGCAGAATTCCGTGAATCATCGAAT
CTTTGAACGCACATTGCGCCCCTTGGTATTCCAGGGGGCATGCCTGTTTGAGCGTCATTTCCTTCTCAAA
CATTCTGTTTGGTAGTGAGTGATACTCTTTGGAGTTAACTTGAAATTGCTGGCCTTTTCATTGGATGTTT
TTTTTCCAAAGAGAGGTTTCTCTGCGTGCCTGAGGTATAATGCAAGTACGGTCGTTTTAGGTTTTACCAA
ATGCGGCTAATC
FASTA

download_db() {
  local db=$1
  local dest="$db_root/$db"
  mkdir -p "$dest"
  echo "DOWNLOAD_BEGIN $db" | tee -a "$out_dir/summary.env"
  azcopy cp "https://${storage_account}.blob.core.windows.net/${container}/${db}/" "$dest/" --recursive=true --log-level=ERROR
  find "$dest" -maxdepth 3 -type f -printf '%P\t%s\n' | sort > "$out_dir/${db}-files.txt"
  echo "DOWNLOAD_END $db" | tee -a "$out_dir/summary.env"
}

run_case() {
  local case_id=$1 db=$2 query=$3 expected_db_len=$4 expected_db_num=$5 expected_top=$6
  local db_path="$db_root/$db/$db/$db"
  local xml="$out_dir/results/${case_id}.xml"
  local info="$out_dir/results/${case_id}-db-info.txt"
  echo "RUN_BEGIN $case_id db=$db" | tee -a "$out_dir/summary.env"
  blastdbcmd -db "$db_path" -info | tee "$info"
  local start_seconds end_seconds
  start_seconds=$(date +%s)
  blastn -query "$query" -db "$db_path" -out "$xml" -outfmt 5 \
    -evalue 10 -max_target_seqs 500 -word_size 28 -dust yes
  end_seconds=$(date +%s)
  printf 'elapsed_seconds=%s\n' "$((end_seconds - start_seconds))" > "$out_dir/results/${case_id}-time.txt"
  python3 - <<'PY' "$xml" "$out_dir/results/${case_id}-summary.json" "$case_id" "$db" "$expected_db_len" "$expected_db_num" "$expected_top"
import json
import sys
import xml.etree.ElementTree as ET

xml_path, out_path, case_id, db, expected_db_len, expected_db_num, expected_top = sys.argv[1:]
root = ET.parse(xml_path).getroot()
hits = root.findall('.//Hit')
hsps = root.findall('.//Hsp')
stats = root.find('.//Statistics')
top = hits[0] if hits else None
def text(node, name):
    found = node.find(name) if node is not None else None
    return found.text if found is not None else None
payload = {
    'case_id': case_id,
    'database': db,
    'hit_count': len(hits),
    'hsp_count': len(hsps),
    'top_hit_id': text(top, 'Hit_id'),
    'top_hit_accession': text(top, 'Hit_accession'),
    'statistics_db_len': text(stats, 'Statistics_db-len'),
    'statistics_db_num': text(stats, 'Statistics_db-num'),
    'statistics_eff_space': text(stats, 'Statistics_eff-space'),
    'expected_db_len': expected_db_len,
    'expected_db_num': expected_db_num,
    'expected_top_hit_accession': expected_top,
}
payload['matches_web_db_len'] = payload['statistics_db_len'] == expected_db_len
payload['matches_web_db_num'] = payload['statistics_db_num'] == expected_db_num
payload['matches_web_top_accession'] = payload['top_hit_accession'] == expected_top
with open(out_path, 'w', encoding='utf-8') as handle:
    json.dump(payload, handle, indent=2)
    handle.write('\n')
print(json.dumps(payload, indent=2))
PY
  echo "RUN_END $case_id" | tee -a "$out_dir/summary.env"
}

download_db 16S_ribosomal_RNA
download_db 18S_fungal_sequences
download_db ITS_RefSeq_Fungi

run_case 16s 16S_ribosomal_RNA "$out_dir/queries/16s.fa" 40051470 27648 NR_025211
run_case 18s 18S_fungal_sequences "$out_dir/queries/18s.fa" 5185702 3907 NG_065576
run_case its ITS_RefSeq_Fungi "$out_dir/queries/its.fa" 12019908 20219 NR_111007

python3 - <<'PY' "$out_dir"
import json
import pathlib
import sys

out_dir = pathlib.Path(sys.argv[1])
cases = []
for path in sorted((out_dir / 'results').glob('*-summary.json')):
    cases.append(json.loads(path.read_text(encoding='utf-8')))
payload = {
    'cases': cases,
    'all_db_stats_match_web': all(c['matches_web_db_len'] and c['matches_web_db_num'] for c in cases),
    'all_top_accessions_match_web': all(c['matches_web_top_accession'] for c in cases),
}
(out_dir / 'summary.json').write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
print(json.dumps(payload, indent=2))
PY

printf 'EQ07_LOCAL_FULL_FINISHED=%s\n' "$(date -u +%Y%m%dT%H%M%SZ)" | tee -a "$out_dir/summary.env"
echo "Evidence: $out_dir"