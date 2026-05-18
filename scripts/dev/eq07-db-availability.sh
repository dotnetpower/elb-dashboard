#!/usr/bin/env bash
set -euo pipefail

stamp=$(date -u +%Y%m%dT%H%M%SZ)
out_dir="/workspace/evidence/eq07-db-availability-${stamp}"
mkdir -p "$out_dir"

namespace=${EQ07_NAMESPACE:-default}
image=${EQ07_ELB_IMAGE:-elbacr01.azurecr.io/ncbi/elb:1.4.0}
selector=${BLAST_NODE_SELECTOR:-workload=blast}
job_prefix="eq07-db-scan-${stamp,,}"
job_prefix=${job_prefix//:/-}
job_prefix=${job_prefix//_/-}

target_dbs=(
  "16S_ribosomal_RNA"
  "18S_fungal_sequences"
  "ITS_RefSeq_Fungi"
  "core_nt_shard_00"
)

printf 'EQ07_DB_AVAILABILITY_STARTED=%s\n' "$stamp" | tee "$out_dir/summary.env"
printf 'NAMESPACE=%s\n' "$namespace" | tee -a "$out_dir/summary.env"
printf 'IMAGE=%s\n' "$image" | tee -a "$out_dir/summary.env"
printf 'BLAST_NODE_SELECTOR=%s\n' "$selector" | tee -a "$out_dir/summary.env"

kubectl get nodes -l "$selector" -o wide | tee "$out_dir/blast-nodes-wide.txt"
mapfile -t nodes < <(kubectl get nodes -l "$selector" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
if [[ ${#nodes[@]} -eq 0 ]]; then
  echo "ERROR: no blast nodes found for selector ${selector}" | tee -a "$out_dir/summary.env"
  exit 1
fi

cleanup() {
  for node in "${nodes[@]}"; do
    safe_node=${node//./-}
    safe_node=${safe_node,,}
    job_name="${job_prefix}-${safe_node}"
    kubectl -n "$namespace" delete job "$job_name" --ignore-not-found=true >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

for node in "${nodes[@]}"; do
  safe_node=${node//./-}
  safe_node=${safe_node,,}
  job_name="${job_prefix}-${safe_node}"
  cat <<YAML | kubectl -n "$namespace" apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job_name}
  labels:
    app: eq07-db-availability
    managed-by: aks-equivalence-runner
spec:
  backoffLimit: 0
  template:
    metadata:
      labels:
        app: eq07-db-availability
        managed-by: aks-equivalence-runner
    spec:
      restartPolicy: Never
      nodeName: ${node}
      tolerations:
        - key: workload
          operator: Equal
          value: blast
          effect: NoSchedule
      containers:
        - name: scan
          image: ${image}
          imagePullPolicy: IfNotPresent
          command: ["/bin/bash", "-lc"]
          args:
            - |
              set -euo pipefail
              cd /blast/blastdb
              echo "NODE=\$(hostname)"
              echo "PWD=\$PWD"
              echo "BLAST_VERSION=\$(blastn -version | head -1 || true)"
              echo "TOP_LEVEL"
              find . -maxdepth 1 -mindepth 1 -printf '%f\t%y\t%s\n' | sort | head -200
              echo "DB_SUMMARY_BEGIN"
              for db in 16S_ribosomal_RNA 18S_fungal_sequences ITS_RefSeq_Fungi core_nt_shard_00; do
                echo "DB=\$db"
                for ext in nal nsq nin nhr nsd nsi nog ndb ntf nto sqlite3 manifest; do
                  count=\$(find . -maxdepth 1 -name "\${db}*.\${ext}" | wc -l)
                  printf '  EXT=%s COUNT=%s\n' "\$ext" "\$count"
                done
                if compgen -G "\${db}.*" >/dev/null || compgen -G "\${db}_v5.*" >/dev/null; then
                  blastdbcmd -db "\$db" -info 2>&1 | sed 's/^/  INFO: /' || true
                else
                  echo "  INFO: files not found"
                fi
              done
              echo "DB_SUMMARY_END"
          volumeMounts:
            - name: blast-dbs
              mountPath: /blast/blastdb
              subPath: blast
      volumes:
        - name: blast-dbs
          hostPath:
            path: /workspace
            type: DirectoryOrCreate
YAML
done

deadline=$((SECONDS + 300))
while (( SECONDS < deadline )); do
  incomplete=0
  for node in "${nodes[@]}"; do
    safe_node=${node//./-}
    safe_node=${safe_node,,}
    job_name="${job_prefix}-${safe_node}"
    succeeded=$(kubectl -n "$namespace" get job "$job_name" -o jsonpath='{.status.succeeded}' 2>/dev/null || true)
    failed=$(kubectl -n "$namespace" get job "$job_name" -o jsonpath='{.status.failed}' 2>/dev/null || true)
    if [[ "$succeeded" != "1" && -z "$failed" ]]; then
      incomplete=$((incomplete + 1))
    fi
  done
  if [[ $incomplete -eq 0 ]]; then
    break
  fi
  sleep 5
done

status_file="$out_dir/job-status.txt"
kubectl -n "$namespace" get jobs -l app=eq07-db-availability -o wide | tee "$status_file"

for node in "${nodes[@]}"; do
  safe_node=${node//./-}
  safe_node=${safe_node,,}
  job_name="${job_prefix}-${safe_node}"
  log_file="$out_dir/${job_name}.log"
  kubectl -n "$namespace" logs "job/${job_name}" > "$log_file" 2>&1 || true
done

python3 - <<'PY' "$out_dir" "${nodes[@]}"
import json
import pathlib
import re
import sys

out_dir = pathlib.Path(sys.argv[1])
nodes = sys.argv[2:]
targets = ["16S_ribosomal_RNA", "18S_fungal_sequences", "ITS_RefSeq_Fungi", "core_nt_shard_00"]
summary = {"nodes": {}, "targets": {name: {"nodes_with_files": 0, "nodes_with_blastdb_info": 0} for name in targets}}

for log_path in sorted(out_dir.glob("eq07-db-scan-*.log")):
    text = log_path.read_text(encoding="utf-8", errors="replace")
    node_match = re.search(r"^NODE=(.+)$", text, re.MULTILINE)
    node = node_match.group(1) if node_match else log_path.stem
    node_info = {"log": log_path.name, "targets": {}}
    for target in targets:
        block_match = re.search(rf"^DB={re.escape(target)}\n(?P<body>.*?)(?=^DB=|^DB_SUMMARY_END)", text, re.MULTILINE | re.DOTALL)
        body = block_match.group("body") if block_match else ""
        has_files = any(
            int(match.group(1)) > 0
            for match in re.finditer(r"COUNT=(\d+)", body)
        )
        has_info = "INFO: Database:" in body or "INFO: Number of letters" in body
        node_info["targets"][target] = {"has_files": has_files, "has_blastdb_info": has_info}
        if has_files:
            summary["targets"][target]["nodes_with_files"] += 1
        if has_info:
            summary["targets"][target]["nodes_with_blastdb_info"] += 1
    summary["nodes"][node] = node_info

summary["node_count"] = len(nodes)
(out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
print(json.dumps(summary, indent=2))
PY

printf 'EQ07_DB_AVAILABILITY_FINISHED=%s\n' "$(date -u +%Y%m%dT%H%M%SZ)" | tee -a "$out_dir/summary.env"
echo "Evidence: $out_dir"