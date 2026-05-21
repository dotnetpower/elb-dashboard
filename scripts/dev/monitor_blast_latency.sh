#!/usr/bin/env bash
# Continuously monitor an already-submitted BLAST job and time the transitions.
# Usage: monitor_blast_latency.sh <full-uuid> [t0_epoch_seconds]
set -euo pipefail

JOB_ID="${1:?usage: $0 <job-uuid> [t0_seconds]}"
T0_SEC="${2:-$(date +%s)}"
API="${API:-http://127.0.0.1:8085}"
SHORT_ID=$(echo "$JOB_ID" | tr -d '-' | cut -c1-12)

ts() { date -u +'%Y-%m-%dT%H:%M:%SZ'; }
now_ms() { date +%s%3N; }
T0_MS=$((T0_SEC * 1000))

echo "Monitoring job $JOB_ID (short=$SHORT_ID), T0=$(date -u -d "@$T0_SEC" +%Y-%m-%dT%H:%M:%SZ)"

LAST_KEY=""
RUNNING_AT=""
EXPORTING_AT=""

while true; do
  T_NOW=$(now_ms)
  DELTA=$((T_NOW - T0_MS))

  DETAIL=$(curl -sS "$API/api/blast/jobs/$JOB_ID?include_database_metadata=false" 2>/dev/null)
  PHASE=$(echo "$DETAIL" | jq -r '.phase // empty')
  STATUS=$(echo "$DETAIL" | jq -r '.status // empty')
  CUSTOM_PHASE=$(echo "$DETAIL" | jq -r '.custom_status.phase // empty')

  LIST=$(curl -sS "$API/api/blast/jobs?limit=20" 2>/dev/null)
  LIST_ROW=$(echo "$LIST" | jq --arg s "$SHORT_ID" '.jobs // [] | map(select(.job_id == $s)) | first // {}')
  L_STATUS=$(echo "$LIST_ROW" | jq -r '.status // empty')
  L_PHASE=$(echo "$LIST_ROW" | jq -r '.phase // empty')

  KEY="$PHASE|$STATUS|$L_PHASE|$L_STATUS"
  if [[ "$KEY" != "$LAST_KEY" ]]; then
    printf '[%s] T+%6dms DETAIL: status=%-12s phase=%-15s custom=%-15s | LIST: status=%-12s phase=%-15s\n' \
      "$(ts)" "$DELTA" "$STATUS" "$PHASE" "$CUSTOM_PHASE" "$L_STATUS" "$L_PHASE"
    LAST_KEY="$KEY"
  fi

  if [[ -z "$RUNNING_AT" && "$PHASE" == "running" ]]; then
    RUNNING_AT=$DELTA
    echo "  --> running phase entered at T+${DELTA}ms"
  fi
  if [[ -z "$EXPORTING_AT" && "$PHASE" == "exporting_results" ]]; then
    EXPORTING_AT=$DELTA
    echo "  --> exporting_results phase entered at T+${DELTA}ms"
  fi

  # Only stop when PHASE is terminal (not when transient status flips)
  if [[ "$PHASE" == "completed" || "$PHASE" == "failed" || "$PHASE" == "cancelled" || "$PHASE" == "submit_failed" ]]; then
    DETAIL_DONE=$DELTA
    echo "  --> terminal phase=$PHASE detected at T+${DELTA}ms"

    LIST_DONE=""
    for ((j=0; j<30; j++)); do
      sleep 2
      T_NOW=$(now_ms)
      DELTA=$((T_NOW - T0_MS))
      LIST=$(curl -sS "$API/api/blast/jobs?limit=20" 2>/dev/null)
      LIST_ROW=$(echo "$LIST" | jq --arg s "$SHORT_ID" '.jobs // [] | map(select(.job_id == $s)) | first // {}')
      L_PHASE=$(echo "$LIST_ROW" | jq -r '.phase // empty')
      L_STATUS=$(echo "$LIST_ROW" | jq -r '.status // empty')
      printf '[%s] T+%6dms LIST after-terminal poll: status=%-12s phase=%-15s\n' "$(ts)" "$DELTA" "$L_STATUS" "$L_PHASE"
      if [[ "$L_PHASE" == "completed" || "$L_PHASE" == "failed" || "$L_PHASE" == "cancelled" || "$L_PHASE" == "submit_failed" ]]; then
        LIST_DONE=$DELTA
        break
      fi
    done

    echo ""
    echo "===== SUMMARY ====="
    echo "job_id           : $JOB_ID"
    echo "final phase      : $PHASE"
    [[ -n "$RUNNING_AT"   ]] && echo "T_phase_running  : ${RUNNING_AT}ms"
    [[ -n "$EXPORTING_AT" ]] && echo "T_phase_export   : ${EXPORTING_AT}ms"
    echo "T_detail_done    : ${DETAIL_DONE}ms"
    if [[ -n "$LIST_DONE" ]]; then
      LAG=$((LIST_DONE - DETAIL_DONE))
      echo "T_list_done      : ${LIST_DONE}ms"
      echo "list_lag         : ${LAG}ms  (Fix #1+#2+#3 shrinks this)"
    else
      echo "T_list_done      : (not observed within 60s)"
    fi
    echo ""
    echo "===== STEP BREAKDOWN ====="
    echo "$DETAIL" | jq '{
      steps: (.custom_status.steps | to_entries | map({step: .key, phase: .value.phase, started_at: .value.started_at, duration_ms: .value.duration_ms}))
    }'
    exit 0
  fi

  sleep 2
done
