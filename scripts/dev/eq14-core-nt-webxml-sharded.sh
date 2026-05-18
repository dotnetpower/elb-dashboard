#!/usr/bin/env bash
set -euo pipefail

stamp=$(date -u +%Y%m%dT%H%M%SZ)
stamp_slug=$(date -u +%Y%m%dt%H%M%Sz)
run_id="eq14-core-nt-webxml-sharded-${stamp_slug}"
out_dir="/workspace/evidence/${run_id}"
work_dir="/workspace/${run_id}-work"
mkdir -p "$out_dir" "$work_dir"

namespace=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace 2>/dev/null || echo elb-equivalence)
tools_cm=${EQ14_TOOLS_CONFIGMAP:-eq14-core-nt-webxml-tools}
storage_account=${EQ14_STORAGE_ACCOUNT:-elbstg01}
results_url=${EQ14_RESULTS_URL:-"https://${storage_account}.blob.core.windows.net/results/${run_id}"}
partition_prefix=${EQ14_PARTITION_PREFIX:-"https://${storage_account}.blob.core.windows.net/blast-db/10shards/core_nt_shard_"}
db_name=${EQ14_DB_NAME:-core_nt}
num_shards=${EQ14_NUM_SHARDS:-10}
searchsp=${EQ14_SEARCHSP:-32156241807668}
taxids=${EQ14_TAXIDS:-10244}
entrez_query=${EQ14_ENTREZ_QUERY:-"txid10244[Organism:exp]"}
max_target_seqs=${EQ14_MAX_TARGET_SEQS:-5000}
web_hitlist_size=${EQ14_WEB_HITLIST_SIZE:-500}
web_expect=${EQ14_WEB_EXPECT:-0.05}
web_word_size=${EQ14_WEB_WORD_SIZE:-28}
web_filter=${EQ14_WEB_FILTER:-L}
web_deadline_seconds=${EQ14_WEB_DEADLINE_SECONDS:-14400}
web_poll_seconds=${EQ14_WEB_POLL_SECONDS:-60}
image=${EQ14_ELB_IMAGE:-elbacr01.azurecr.io/ncbi/elb:1.4.0}
export AZCOPY_AUTO_LOGIN_TYPE=${AZCOPY_AUTO_LOGIN_TYPE:-MSI}

printf 'EQ14_CORE_NT_WEBXML_STARTED=%s\n' "$stamp" | tee "$out_dir/summary.env"
printf 'NODE_NAME=%s\n' "${NODE_NAME:-unknown}" | tee -a "$out_dir/summary.env"
printf 'RESULTS_URL=%s\n' "$results_url" | tee -a "$out_dir/summary.env"
printf 'PARTITION_PREFIX=%s\n' "$partition_prefix" | tee -a "$out_dir/summary.env"
printf 'NUM_SHARDS=%s\n' "$num_shards" | tee -a "$out_dir/summary.env"
printf 'SEARCHSP=%s\n' "$searchsp" | tee -a "$out_dir/summary.env"
printf 'TAXIDS=%s\n' "$taxids" | tee -a "$out_dir/summary.env"
printf 'ENTREZ_QUERY=%s\n' "$entrez_query" | tee -a "$out_dir/summary.env"
printf 'WEB_EXPECT=%s\n' "$web_expect" | tee -a "$out_dir/summary.env"
printf 'WEB_HITLIST_SIZE=%s\n' "$web_hitlist_size" | tee -a "$out_dir/summary.env"
printf 'MAX_TARGET_SEQS=%s\n' "$max_target_seqs" | tee -a "$out_dir/summary.env"

for key in MPXV_F3L.fa compare-blast-web-xml-outfmt6.py merge-sharded-results.sh; do
  kubectl -n "$namespace" get configmap "$tools_cm" \
    -o "go-template={{ index .data \"${key}\" }}" > "$work_dir/$key"
done
chmod +x "$work_dir/compare-blast-web-xml-outfmt6.py" "$work_dir/merge-sharded-results.sh"

web_xml="$out_dir/web-blast.xml"
web_csv="$out_dir/web-blast-from-xml.csv"
web_accessions="$out_dir/web-top-accessions.txt"
widepool="$out_dir/sharded-widepool.outfmt6"
strict_outfmt6="$out_dir/strict-web-oracle-merged.outfmt6"

submit_web_blast() {
  python3 - <<'PY' "$work_dir/MPXV_F3L.fa" "$out_dir/web-put-response.txt" "$out_dir/web-rid.env" "$entrez_query" "$web_expect" "$web_word_size" "$web_hitlist_size" "$web_filter"
from __future__ import annotations

import sys
import urllib.parse
import urllib.request
from pathlib import Path

query_path, response_path, rid_env_path, entrez_query, expect, word_size, hitlist_size, filter_value = sys.argv[1:]
query = Path(query_path).read_text(encoding="utf-8")
params = {
    "CMD": "Put",
    "PROGRAM": "blastn",
    "DATABASE": "core_nt",
    "QUERY": query,
    "MEGABLAST": "on",
    "EXPECT": expect,
    "WORD_SIZE": word_size,
    "HITLIST_SIZE": hitlist_size,
    "FILTER": filter_value,
    "ENTREZ_QUERY": entrez_query,
    "TOOL": "elb-dashboard-equivalence",
}
email = ""
if email:
    params["EMAIL"] = email
encoded = urllib.parse.urlencode(params).encode("utf-8")
request = urllib.request.Request(
    "https://blast.ncbi.nlm.nih.gov/Blast.cgi",
    data=encoded,
    headers={"User-Agent": "elb-dashboard-equivalence/1.0"},
)
with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - NCBI public endpoint.
    text = response.read().decode("utf-8", errors="replace")
Path(response_path).write_text(text, encoding="utf-8")
rid = ""
rtoe = ""
for line in text.splitlines():
    stripped = line.strip()
    if stripped.startswith("RID ="):
        rid = stripped.split("=", 1)[1].strip()
    if stripped.startswith("RTOE ="):
        rtoe = stripped.split("=", 1)[1].strip()
if not rid:
    raise SystemExit("NCBI Web BLAST submission did not return RID")
Path(rid_env_path).write_text(f"WEB_RID={rid}\nWEB_RTOE={rtoe}\n", encoding="utf-8")
print(f"WEB_RID={rid}")
print(f"WEB_RTOE={rtoe}")
PY
}

poll_web_blast() {
  local rid status deadline
  rid=$(awk -F= '$1=="WEB_RID" {print $2}' "$out_dir/web-rid.env")
  deadline=$((SECONDS + web_deadline_seconds))
  while (( SECONDS < deadline )); do
    status=$(python3 - <<'PY' "$rid" "$out_dir/web-search-info.txt"
from __future__ import annotations

import sys
import urllib.parse
import urllib.request
from pathlib import Path

rid, out_path = sys.argv[1:]
params = urllib.parse.urlencode({"CMD": "Get", "RID": rid, "FORMAT_OBJECT": "SearchInfo"})
url = f"https://blast.ncbi.nlm.nih.gov/Blast.cgi?{params}"
request = urllib.request.Request(url, headers={"User-Agent": "elb-dashboard-equivalence/1.0"})
with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - NCBI public endpoint.
    text = response.read().decode("utf-8", errors="replace")
Path(out_path).write_text(text, encoding="utf-8")
status = "UNKNOWN"
for line in text.splitlines():
    stripped = line.strip()
    if stripped.startswith("Status="):
        status = stripped.split("=", 1)[1].strip()
print(status)
PY
)
    echo "WEB_PROGRESS rid=${rid} status=${status}" | tee -a "$out_dir/summary.env"
    case "$status" in
      READY)
        python3 - <<'PY' "$rid" "$web_xml"
from __future__ import annotations

import sys
import urllib.parse
import urllib.request
from pathlib import Path

rid, out_path = sys.argv[1:]
params = urllib.parse.urlencode({"CMD": "Get", "RID": rid, "FORMAT_TYPE": "XML"})
url = f"https://blast.ncbi.nlm.nih.gov/Blast.cgi?{params}"
request = urllib.request.Request(url, headers={"User-Agent": "elb-dashboard-equivalence/1.0"})
with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310 - NCBI public endpoint.
    text = response.read().decode("utf-8", errors="replace")
Path(out_path).write_text(text, encoding="utf-8")
PY
        return 0
        ;;
      FAILED|UNKNOWN)
        echo "ERROR: Web BLAST RID ${rid} reached status ${status}" >&2
        return 1
        ;;
    esac
    sleep "$web_poll_seconds"
  done
  echo "ERROR: timed out waiting for Web BLAST RID" >&2
  return 1
}

submit_web_blast | tee -a "$out_dir/summary.env"

query_cm="${run_id}-query"
kubectl -n default create configmap "$query_cm" \
  --from-file=MPXV_F3L.fa="$work_dir/MPXV_F3L.fa" \
  --dry-run=client -o yaml | kubectl apply -f -

azcopy login --identity >/dev/null
azcopy cp "$work_dir/MPXV_F3L.fa" "${results_url}/query_batches/batch_000.fa" --log-level=ERROR

mapfile -t nodes < <(kubectl get nodes -l workload=blast -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort)
if (( ${#nodes[@]} < num_shards )); then
  echo "ERROR: need ${num_shards} blast nodes, found ${#nodes[@]}" >&2
  exit 1
fi
printf '%s\n' "${nodes[@]}" > "$out_dir/blast-nodes.txt"

cleanup_child_jobs() {
  kubectl -n default delete jobs -l "app=eq14-core-nt-webxml,run=${run_id}" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n default delete configmap "$query_cm" --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup_child_jobs EXIT

for idx in $(seq 0 $((num_shards - 1))); do
  shard=$(printf '%02d' "$idx")
  node=${nodes[$idx]}
  job_name="eq14-core-nt-s${shard}-${stamp_slug}"
  db_shard="${db_name}_shard_${shard}"
  shard_results="${results_url}/shard_${shard}"
  cat <<YAML | kubectl -n default apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job_name}
  labels:
    app: eq14-core-nt-webxml
    run: ${run_id}
    shard: "${idx}"
spec:
  backoffLimit: 1
  template:
    metadata:
      labels:
        app: eq14-core-nt-webxml
        run: ${run_id}
        shard: "${idx}"
    spec:
      restartPolicy: Never
      nodeName: ${node}
      tolerations:
        - key: workload
          operator: Equal
          value: blast
          effect: NoSchedule
      containers:
        - name: blast
          image: ${image}
          imagePullPolicy: IfNotPresent
          workingDir: /blast/blastdb
          resources:
            requests:
              cpu: "6"
              memory: 4G
            limits:
              cpu: "8"
              memory: 126Gi
          env:
            - name: AZCOPY_AUTO_LOGIN_TYPE
              value: MSI
            - name: ELB_SHARD_IDX
              value: "${shard}"
            - name: ELB_PARTITION_PREFIX
              value: "${partition_prefix}"
            - name: ELB_DB
              value: "${db_shard}"
            - name: ELB_DB_MOL_TYPE
              value: nucl
            - name: ELB_RESULTS
              value: "${shard_results}"
          command:
            - /bin/bash
            - -lc
            - |
              set -euo pipefail
              azcopy login --identity >/dev/null
              cd /blast/blastdb
              if ! blastdbcmd -db "${db_shard}" -dbtype nucl -info >/tmp/db-info.txt 2>/tmp/db-info.err; then
                /scripts/init-db-shard-aks.sh > /tmp/init-db-shard.log 2>&1
                /scripts/blast-vmtouch-aks.sh > /tmp/vmtouch.log 2>&1 || true
                blastdbcmd -db "${db_shard}" -dbtype nucl -info > /tmp/db-info.txt
              fi
              blastn -query /query/MPXV_F3L.fa -db "${db_shard}" \
                -num_threads 8 \
                -evalue ${web_expect} \
                -max_target_seqs ${max_target_seqs} \
                -taxids ${taxids} \
                -outfmt '6 std score' \
                -word_size ${web_word_size} \
                -searchsp ${searchsp} \
                -dust yes \
                -soft_masking false \
                -out /tmp/widepool.outfmt6
              gzip -c /tmp/widepool.outfmt6 > /tmp/widepool.outfmt6.gz
              azcopy cp /tmp/widepool.outfmt6.gz "${shard_results}/widepool.outfmt6.gz" --log-level=ERROR
              azcopy cp /tmp/db-info.txt "${shard_results}/db-info.txt" --log-level=ERROR
              test ! -f /tmp/init-db-shard.log || azcopy cp /tmp/init-db-shard.log "${shard_results}/init-db-shard.log" --log-level=ERROR
              test ! -f /tmp/vmtouch.log || azcopy cp /tmp/vmtouch.log "${shard_results}/vmtouch.log" --log-level=ERROR
          volumeMounts:
            - name: blast-dbs
              mountPath: /blast/blastdb
              subPath: blast
            - name: scripts
              mountPath: /scripts
              readOnly: true
            - name: query
              mountPath: /query
              readOnly: true
      volumes:
        - name: blast-dbs
          hostPath:
            path: /workspace
            type: DirectoryOrCreate
        - name: scripts
          configMap:
            name: elb-scripts
            defaultMode: 0755
        - name: query
          configMap:
            name: ${query_cm}
YAML
done

deadline=$((SECONDS + 3600))
while (( SECONDS < deadline )); do
  succeeded=$(kubectl -n default get jobs -l "app=eq14-core-nt-webxml,run=${run_id}" -o jsonpath='{range .items[*]}{.status.succeeded}{"\n"}{end}' | awk '$1==1 {count++} END {print count+0}')
  failed=$(kubectl -n default get jobs -l "app=eq14-core-nt-webxml,run=${run_id}" -o jsonpath='{range .items[*]}{.status.failed}{"\n"}{end}' | awk '$1>0 {count++} END {print count+0}')
  echo "SHARD_PROGRESS succeeded=${succeeded}/${num_shards} failed=${failed}" | tee -a "$out_dir/summary.env"
  if (( failed > 0 )); then
    kubectl -n default get pods -l "app=eq14-core-nt-webxml,run=${run_id}" -o wide > "$out_dir/failed-pods.txt" || true
    exit 1
  fi
  if (( succeeded == num_shards )); then
    break
  fi
  sleep 20
done
if (( succeeded != num_shards )); then
  echo "ERROR: timed out waiting for sharded Jobs" >&2
  kubectl -n default get jobs,pods -l "app=eq14-core-nt-webxml,run=${run_id}" -o wide > "$out_dir/timeout-jobs-pods.txt" || true
  exit 1
fi

poll_web_blast

python3 - <<'PY' "$web_xml" "$web_csv" "$web_accessions"
from __future__ import annotations

import csv
import sys
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

xml_path, csv_path, accessions_path = map(Path, sys.argv[1:])
root = ET.parse(xml_path).getroot()
rows = []
for hit in root.findall(".//Iteration_hits/Hit"):
    hsp = hit.find("Hit_hsps/Hsp")
    if hsp is None:
        continue
    identity = int(hsp.findtext("Hsp_identity") or "0")
    align_len = int(hsp.findtext("Hsp_align-len") or "0")
    gaps = int(hsp.findtext("Hsp_gaps") or "0")
    accession = (hit.findtext("Hit_accession") or "").strip()
    rows.append({
        "rank": len(rows) + 1,
        "accession": accession,
        "identity_pct": format((Decimal(identity) * Decimal(100) / Decimal(align_len)).quantize(Decimal("0.001")), "f") if align_len else "0",
        "align_length": align_len,
        "mismatches": max(0, align_len - identity - gaps),
        "gaps": gaps,
        "query_from": hsp.findtext("Hsp_query-from") or "",
        "query_to": hsp.findtext("Hsp_query-to") or "",
        "hit_from": hsp.findtext("Hsp_hit-from") or "",
        "hit_to": hsp.findtext("Hsp_hit-to") or "",
        "evalue": hsp.findtext("Hsp_evalue") or "",
        "bits": hsp.findtext("Hsp_bit-score") or "",
        "score": hsp.findtext("Hsp_score") or "",
    })
with csv_path.open("w", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["rank", "accession"])
    writer.writeheader()
    writer.writerows(rows)
accessions_path.write_text("\n".join(str(row["accession"]) for row in rows if row["accession"]) + "\n", encoding="utf-8")
print(f"WEB_XML_ROWS={len(rows)}")
PY

kubectl -n default get jobs,pods -l "app=eq14-core-nt-webxml,run=${run_id}" -o wide > "$out_dir/jobs-pods.txt"
azcopy list "$results_url" --machine-readable > "$out_dir/blob-list.txt"
azcopy cp "$results_url" "$work_dir/download/" --recursive=true --log-level=ERROR

download_root="$work_dir/download"
if [[ -d "$download_root/$run_id" ]]; then
  download_root="$download_root/$run_id"
fi

: > "$widepool"
for idx in $(seq 0 $((num_shards - 1))); do
  shard=$(printf '%02d' "$idx")
  gzip -cd "$download_root/shard_${shard}/widepool.outfmt6.gz" >> "$widepool"
done

python3 "$work_dir/compare-blast-web-xml-outfmt6.py" \
  --web-xml "$web_xml" \
  --candidate "$widepool" \
  --accept-tie-window \
  --json "$out_dir/web-xml-vs-widepool.json" \
  > "$out_dir/web-xml-vs-widepool.stdout" || true

ELB_TIE_ORDER_FILE="$web_accessions" ELB_TIE_ORDER_STRICT=1 \
  "$work_dir/merge-sharded-results.sh" \
  "$widepool" \
  "$out_dir/strict-web-oracle-merged.out.gz" \
  "$out_dir/strict-web-oracle-merge-report.json" \
  "$num_shards" \
  blastn \
  "-outfmt 6 -max_target_seqs ${web_hitlist_size}" \
  > "$out_dir/strict-web-oracle-merge.stdout" \
  2> "$out_dir/strict-web-oracle-merge.stderr"
gzip -cd "$out_dir/strict-web-oracle-merged.out.gz" > "$strict_outfmt6"

python3 "$work_dir/compare-blast-web-xml-outfmt6.py" \
  --web-xml "$web_xml" \
  --candidate "$strict_outfmt6" \
  --json "$out_dir/web-xml-vs-strict-oracle.json" \
  > "$out_dir/web-xml-vs-strict-oracle.stdout" || true

python3 - <<'PY' "$out_dir/web-rid.env" "$out_dir/web-xml-vs-widepool.json" "$out_dir/web-xml-vs-strict-oracle.json" "$out_dir/strict-web-oracle-merge-report.json" "$out_dir/summary.json"
from __future__ import annotations

import json
import sys
from pathlib import Path

rid_env, widepool_path, strict_path, merge_path, summary_path = map(Path, sys.argv[1:])
rid = ""
for line in rid_env.read_text(encoding="utf-8").splitlines():
    if line.startswith("WEB_RID="):
        rid = line.split("=", 1)[1]
widepool = json.loads(widepool_path.read_text(encoding="utf-8"))
strict = json.loads(strict_path.read_text(encoding="utf-8"))
merge = json.loads(merge_path.read_text(encoding="utf-8"))
summary = {
    "web_rid": rid,
    "widepool": {
        "equivalent": widepool["equivalent"],
        "tie_window_equivalent": widepool["tie_window_equivalent"],
        "web_rows": widepool["web_rows"],
        "candidate_rows": widepool["candidate_rows"],
        "shared_accessions": widepool["shared_accessions"],
        "web_only": widepool["web_only"],
        "candidate_only": widepool["candidate_only"],
        "top10_overlap": widepool["top10_overlap"],
        "top100_overlap": widepool["top100_overlap"],
        "value_mismatch_count": widepool["value_mismatch_count"],
        "first_order_mismatch": widepool["first_order_mismatch"],
    },
    "strict_web_oracle": {
        "equivalent": strict["equivalent"],
        "exact_order": strict["exact_order"],
        "web_rows": strict["web_rows"],
        "candidate_rows": strict["candidate_rows"],
        "shared_accessions": strict["shared_accessions"],
        "web_only": strict["web_only"],
        "candidate_only": strict["candidate_only"],
        "top10_overlap": strict["top10_overlap"],
        "top100_overlap": strict["top100_overlap"],
        "value_mismatch_count": strict["value_mismatch_count"],
        "merge_total_input_hits": merge["total_input_hits"],
        "merge_total_output_hits": merge["total_output_hits"],
        "merge_tie_cutoff_overflow_count": merge["tie_cutoff_overflow_count"],
        "merge_tie_order_oracle_accessions": merge["tie_order_oracle_accessions"],
        "merge_tie_order_oracle_strict": merge["tie_order_oracle_strict"],
    },
}
summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(summary, indent=2, sort_keys=True))
PY

printf 'EQ14_CORE_NT_WEBXML_FINISHED=%s\n' "$(date -u +%Y%m%dT%H%M%SZ)" | tee -a "$out_dir/summary.env"
echo "Evidence: $out_dir"