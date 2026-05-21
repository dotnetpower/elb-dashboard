#!/usr/bin/env bash
# Measure end-to-end BLAST submit -> completed perceived latency against the local api.
# Logs every poll with epoch + delta-from-submit + status/phase.
set -euo pipefail

API="${API:-http://127.0.0.1:8085}"
SUB="b052302c-4c8d-49a4-aa2f-9d60a7301a80"
RG="rg-elb-01"
CLUSTER="elb-cluster"
STG="elbstg01"
ACR_NAME="elbacr01"
ACR_RG="rg-elbacr-01"
DB="core_nt"
QUERY="queries/uploads/10949573-3997-4e96-9bfb-d6b8f61c20c5/query.fa"

ts() { date -u +'%Y-%m-%dT%H:%M:%SZ'; }
now_ms() { date +%s%3N; }

T0=$(now_ms)

echo "[$(ts)] T+0    submitting POST /api/blast/submit ..."
SUBMIT_RESP=$(curl -sS -X POST "$API/api/blast/submit" \
  -H 'content-type: application/json' \
  --data @- <<JSON
{
  "subscription_id": "$SUB",
  "resource_group": "$RG",
  "cluster_name": "$CLUSTER",
  "storage_account": "$STG",
  "program": "blastn",
  "database": "$DB",
  "query_file": "$QUERY",
  "shard_sets": [10],
  "query_count": 1,
  "options": {
    "acr_name": "$ACR_NAME",
    "acr_resource_group": "$ACR_RG",
    "machine_type": "Standard_E16s_v5",
    "num_nodes": 10,
    "shard_sets": [10],
    "sharding_mode": "precise",
    "query_count": 1,
    "db_sharded": true,
    "db_auto_partition": true,
    "use_local_ssd": true,
    "reuse": true,
    "enable_warmup": true,
    "pd_size": "2048Gi",
    "mem_request": "8Gi",
    "mem_limit": "24Gi",
    "low_complexity_filter": true,
    "max_target_seqs": 100,
    "outfmt": "5",
    "word_size": 28,
    "evalue": 0.05,
    "db_effective_search_space": 32156241807668
  }
}
JSON
)
T_SUBMIT=$(now_ms)
echo "[$(ts)] T+$((T_SUBMIT - T0))ms submit returned: $SUBMIT_RESP"

JOB_ID=$(echo "$SUBMIT_RESP" | jq -r '.job_id // .id // empty')
if [[ -z "$JOB_ID" ]]; then
  echo "ERROR: no job_id in response" >&2
  exit 1
fi
# Dashboard LIST endpoint uses the 12-hex short id (uuid stripped of dashes).
SHORT_ID=$(echo "$JOB_ID" | tr -d '-' | cut -c1-12)
echo "[$(ts)] job_id=$JOB_ID short=$SHORT_ID"

LAST_PHASE=""
LAST_STATUS=""
RUNNING_AT=""
COMPLETED_AT=""

for ((i=0; i<400; i++)); do
  sleep 2
  T_NOW=$(now_ms)
  DELTA=$((T_NOW - T0))

  # Probe via LIST endpoint (this is what dashboard uses)
  LIST_ROW=$(curl -sS "$API/api/blast/jobs?limit=20" 2>/dev/null \
    | jq --arg short "$SHORT_ID" '.jobs // [] | map(select(.job_id == $short)) | first // {}')
  LIST_STATUS=$(echo "$LIST_ROW" | jq -r '.status // empty')
  LIST_PHASE=$(echo "$LIST_ROW" | jq -r '.phase // empty')

  # Probe via DETAIL endpoint
  DETAIL=$(curl -sS "$API/api/blast/jobs/$JOB_ID?include_database_metadata=false" 2>/dev/null)
  D_STATUS=$(echo "$DETAIL" | jq -r '.status // empty')
  D_PHASE=$(echo "$DETAIL" | jq -r '.phase // empty')

  if [[ "$D_PHASE" != "$LAST_PHASE" || "$D_STATUS" != "$LAST_STATUS" ]]; then
    echo "[$(ts)] T+${DELTA}ms LIST: status=$LIST_STATUS phase=$LIST_PHASE | DETAIL: status=$D_STATUS phase=$D_PHASE"
    LAST_PHASE="$D_PHASE"
    LAST_STATUS="$D_STATUS"
  fi

  if [[ -z "$RUNNING_AT" && "$D_PHASE" == "running" ]]; then
    RUNNING_AT=$DELTA
    echo "[$(ts)] T+${DELTA}ms ===> detail phase=running"
  fi

  if [[ "$D_STATUS" == "completed" || "$D_STATUS" == "failed" || "$D_STATUS" == "cancelled" ]]; then
    DETAIL_COMPLETED_AT=$DELTA
    # Now wait until LIST also reflects completed
    LIST_COMPLETED_AT=""
    for ((j=0; j<30; j++)); do
      sleep 2
      T_NOW=$(now_ms)
      DELTA=$((T_NOW - T0))
      LIST_ROW=$(curl -sS "$API/api/blast/jobs?limit=20" 2>/dev/null \
        | jq --arg short "$SHORT_ID" '.jobs // [] | map(select(.job_id == $short)) | first // {}')
      L_STATUS=$(echo "$LIST_ROW" | jq -r '.status // empty')
      L_PHASE=$(echo "$LIST_ROW" | jq -r '.phase // empty')
      echo "[$(ts)] T+${DELTA}ms LIST poll: status=$L_STATUS phase=$L_PHASE"
      if [[ "$L_STATUS" == "completed" || "$L_STATUS" == "failed" || "$L_STATUS" == "cancelled" ]]; then
        LIST_COMPLETED_AT=$DELTA
        break
      fi
    done
    echo ""
    echo "=== SUMMARY ==="
    echo "job_id           : $JOB_ID"
    echo "T_submit_resp    : $((T_SUBMIT - T0))ms"
    [[ -n "$RUNNING_AT" ]] && echo "T_phase_running  : ${RUNNING_AT}ms"
    echo "T_detail_done    : ${DETAIL_COMPLETED_AT}ms ($D_STATUS)"
    if [[ -n "$LIST_COMPLETED_AT" ]]; then
      LAG=$((LIST_COMPLETED_AT - DETAIL_COMPLETED_AT))
      echo "T_list_done      : ${LIST_COMPLETED_AT}ms"
      echo "list_lag         : ${LAG}ms (this is what fix #1+#2+#3 shrinks)"
    else
      echo "T_list_done      : (not observed within 60s)"
    fi
    # Pull K8s job runtime and step breakdown
    echo ""
    echo "=== STEP BREAKDOWN ==="
    echo "$DETAIL" | jq '{
      total_workflow_ms: .custom_status.duration_ms,
      steps: (.custom_status.steps | to_entries | map({step: .key, phase: .value.phase, started_at: .value.started_at, duration_ms: .value.duration_ms}))
    }'
    exit 0
  fi
done

echo "TIMEOUT: gave up after 400 polls (~800s)"
exit 2
