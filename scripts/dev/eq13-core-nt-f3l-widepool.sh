#!/usr/bin/env bash
set -euo pipefail

stamp=$(date -u +%Y%m%dT%H%M%SZ)
stamp_slug=$(date -u +%Y%m%dt%H%M%Sz)
run_id="eq13-core-nt-f3l-widepool-${stamp_slug}"
out_dir="/workspace/evidence/${run_id}"
work_dir="/workspace/${run_id}-work"
mkdir -p "$out_dir" "$work_dir"

namespace=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace 2>/dev/null || echo elb-equivalence)
tools_cm=${EQ13_TOOLS_CONFIGMAP:-eq13-core-nt-tools}
storage_account=${EQ13_STORAGE_ACCOUNT:-elbstg01}
results_url=${EQ13_RESULTS_URL:-"https://${storage_account}.blob.core.windows.net/results/${run_id}"}
partition_prefix=${EQ13_PARTITION_PREFIX:-"https://${storage_account}.blob.core.windows.net/blast-db/10shards/core_nt_shard_"}
db_name=${EQ13_DB_NAME:-core_nt}
num_shards=${EQ13_NUM_SHARDS:-10}
searchsp=${EQ13_SEARCHSP:-32156241807668}
taxids=${EQ13_TAXIDS:-10244}
max_target_seqs=${EQ13_MAX_TARGET_SEQS:-5000}
image=${EQ13_ELB_IMAGE:-elbacr01.azurecr.io/ncbi/elb:1.4.0}
export AZCOPY_AUTO_LOGIN_TYPE=${AZCOPY_AUTO_LOGIN_TYPE:-MSI}

printf 'EQ13_CORE_NT_STARTED=%s\n' "$stamp" | tee "$out_dir/summary.env"
printf 'NODE_NAME=%s\n' "${NODE_NAME:-unknown}" | tee -a "$out_dir/summary.env"
printf 'RESULTS_URL=%s\n' "$results_url" | tee -a "$out_dir/summary.env"
printf 'PARTITION_PREFIX=%s\n' "$partition_prefix" | tee -a "$out_dir/summary.env"
printf 'NUM_SHARDS=%s\n' "$num_shards" | tee -a "$out_dir/summary.env"
printf 'SEARCHSP=%s\n' "$searchsp" | tee -a "$out_dir/summary.env"
printf 'TAXIDS=%s\n' "$taxids" | tee -a "$out_dir/summary.env"
printf 'MAX_TARGET_SEQS=%s\n' "$max_target_seqs" | tee -a "$out_dir/summary.env"

for key in MPXV_F3L.fa blast_inclusive_F3L_928998.csv compare-blast-web-csv.py merge-sharded-results.sh; do
  kubectl -n "$namespace" get configmap "$tools_cm" \
    -o "go-template={{ index .data \"${key}\" }}" > "$work_dir/$key"
done
chmod +x "$work_dir/compare-blast-web-csv.py" "$work_dir/merge-sharded-results.sh"

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
  kubectl -n default delete jobs -l "app=eq13-core-nt-widepool,run=${run_id}" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n default delete configmap "$query_cm" --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup_child_jobs EXIT

for idx in $(seq 0 $((num_shards - 1))); do
  shard=$(printf '%02d' "$idx")
  node=${nodes[$idx]}
  job_name="eq13-core-nt-s${shard}-${stamp_slug}"
  db_shard="${db_name}_shard_${shard}"
  shard_results="${results_url}/shard_${shard}"
  cat <<YAML | kubectl -n default apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job_name}
  labels:
    app: eq13-core-nt-widepool
    run: ${run_id}
    shard: "${idx}"
spec:
  backoffLimit: 1
  template:
    metadata:
      labels:
        app: eq13-core-nt-widepool
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
                -evalue 10 \
                -max_target_seqs ${max_target_seqs} \
                -taxids ${taxids} \
                -outfmt '6 std score' \
                -word_size 28 \
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
  succeeded=$(kubectl -n default get jobs -l "app=eq13-core-nt-widepool,run=${run_id}" -o jsonpath='{range .items[*]}{.status.succeeded}{"\n"}{end}' | awk '$1==1 {count++} END {print count+0}')
  failed=$(kubectl -n default get jobs -l "app=eq13-core-nt-widepool,run=${run_id}" -o jsonpath='{range .items[*]}{.status.failed}{"\n"}{end}' | awk '$1>0 {count++} END {print count+0}')
  echo "WIDEPOOL_PROGRESS succeeded=${succeeded}/${num_shards} failed=${failed}" | tee -a "$out_dir/summary.env"
  if (( failed > 0 )); then
    kubectl -n default get pods -l "app=eq13-core-nt-widepool,run=${run_id}" -o wide > "$out_dir/failed-pods.txt" || true
    exit 1
  fi
  if (( succeeded == num_shards )); then
    break
  fi
  sleep 20
done
if (( succeeded != num_shards )); then
  echo "ERROR: timed out waiting for widepool shard jobs" >&2
  kubectl -n default get jobs,pods -l "app=eq13-core-nt-widepool,run=${run_id}" -o wide > "$out_dir/timeout-jobs-pods.txt" || true
  exit 1
fi

kubectl -n default get jobs,pods -l "app=eq13-core-nt-widepool,run=${run_id}" -o wide > "$out_dir/jobs-pods.txt"
azcopy list "$results_url" --machine-readable > "$out_dir/blob-list.txt"
azcopy cp "$results_url" "$work_dir/download/" --recursive=true --log-level=ERROR

download_root="$work_dir/download"
if [[ -d "$download_root/$run_id" ]]; then
  download_root="$download_root/$run_id"
fi

widepool="$out_dir/widepool.outfmt6"
: > "$widepool"
for idx in $(seq 0 $((num_shards - 1))); do
  shard=$(printf '%02d' "$idx")
  gzip -cd "$download_root/shard_${shard}/widepool.outfmt6.gz" >> "$widepool"
done

python3 "$work_dir/compare-blast-web-csv.py" \
  --web-csv "$work_dir/blast_inclusive_F3L_928998.csv" \
  --candidate "$widepool" \
  --accept-tie-window \
  --json "$out_dir/inclusive-web-vs-widepool.json" \
  > "$out_dir/inclusive-web-vs-widepool.stdout" || true

python3 - <<'PY' "$work_dir/blast_inclusive_F3L_928998.csv" "$out_dir/web-top500-accessions.txt"
import csv
import sys
from pathlib import Path

csv_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])
with csv_path.open(newline='', encoding='utf-8-sig') as handle:
    reader = csv.DictReader(handle)
    accessions = [(row.get('accession') or '').strip() for row in reader]
out_path.write_text('\n'.join(accession for accession in accessions if accession) + '\n', encoding='utf-8')
PY

ELB_TIE_ORDER_FILE="$out_dir/web-top500-accessions.txt" ELB_TIE_ORDER_STRICT=1 \
  "$work_dir/merge-sharded-results.sh" \
  "$widepool" \
  "$out_dir/strict-web-oracle-merged.out.gz" \
  "$out_dir/strict-web-oracle-merge-report.json" \
  "$num_shards" \
  blastn \
  "-outfmt 6 -max_target_seqs 500" \
  > "$out_dir/strict-web-oracle-merge.stdout" \
  2> "$out_dir/strict-web-oracle-merge.stderr"
gzip -cd "$out_dir/strict-web-oracle-merged.out.gz" > "$out_dir/strict-web-oracle-merged.outfmt6"

python3 "$work_dir/compare-blast-web-csv.py" \
  --web-csv "$work_dir/blast_inclusive_F3L_928998.csv" \
  --candidate "$out_dir/strict-web-oracle-merged.outfmt6" \
  --json "$out_dir/inclusive-web-vs-strict-oracle.json" \
  > "$out_dir/inclusive-web-vs-strict-oracle.stdout" || true

python3 - <<'PY' "$out_dir/inclusive-web-vs-widepool.json" "$out_dir/inclusive-web-vs-strict-oracle.json" "$out_dir/strict-web-oracle-merge-report.json" "$out_dir/summary.json"
import json
import sys
from pathlib import Path

widepool = json.loads(Path(sys.argv[1]).read_text())
strict = json.loads(Path(sys.argv[2]).read_text())
merge = json.loads(Path(sys.argv[3]).read_text())
summary = {
    'widepool': {
        'candidate_rows': widepool['candidate_rows'],
        'web_rows': widepool['web_rows'],
        'shared_accessions': widepool['shared_accessions'],
        'web_only': widepool['web_only'],
        'candidate_only': widepool['candidate_only'],
        'value_mismatch_count': widepool['value_mismatch_count'],
        'tie_window_equivalent': widepool['tie_window_equivalent'],
        'top10_overlap': widepool['top10_overlap'],
        'first_order_mismatch': widepool['first_order_mismatch'],
    },
    'strict_web_oracle': {
        'equivalent': strict['equivalent'],
        'exact_order': strict['exact_order'],
        'candidate_rows': strict['candidate_rows'],
        'shared_accessions': strict['shared_accessions'],
        'web_only': strict['web_only'],
        'candidate_only': strict['candidate_only'],
        'value_mismatch_count': strict['value_mismatch_count'],
        'top10_overlap': strict['top10_overlap'],
        'merge_total_input_hits': merge['total_input_hits'],
        'merge_total_output_hits': merge['total_output_hits'],
        'merge_tie_cutoff_overflow_count': merge['tie_cutoff_overflow_count'],
        'merge_tie_order_oracle_accessions': merge['tie_order_oracle_accessions'],
        'merge_tie_order_oracle_strict': merge['tie_order_oracle_strict'],
    },
}
Path(sys.argv[4]).write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n', encoding='utf-8')
print(json.dumps(summary, indent=2, sort_keys=True))
PY

printf 'EQ13_CORE_NT_FINISHED=%s\n' "$(date -u +%Y%m%dT%H%M%SZ)" | tee -a "$out_dir/summary.env"
echo "Evidence: $out_dir"