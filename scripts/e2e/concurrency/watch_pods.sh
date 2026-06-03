#!/usr/bin/env bash
# Sample elastic-blast pods/jobs on the live AKS cluster at a fixed cadence.
#
# Responsibility: Write a NDJSON timeline of pod phase counts so we capture the
# *real* concurrency (RUNNING vs Pending) the elb-openapi service achieves on
# blastpool, independent of what the API status endpoint reports.
# Edit boundaries: read-only kubectl against the kubeconfig in $KUBECONFIG.
# Never mutates the cluster. Stops when the marker file is removed or on SIGTERM.
# Usage: KUBECONFIG=/tmp/elb-kubeconfig watch_pods.sh <out.ndjson> [interval_s]
set -euo pipefail

OUT="${1:?usage: watch_pods.sh <out.ndjson> [interval_s]}"
INTERVAL="${2:-3}"
# elastic-blast batch pods carry label app=blast (NOT app=elastic-blast),
# plus elb-job-id and shard labels. Verified on elb-cluster-02 2026-06-03.
SELECTOR="${ELB_POD_SELECTOR:-app=blast}"

mkdir -p "$(dirname "$OUT")"
echo "[watch_pods] selector=$SELECTOR interval=${INTERVAL}s -> $OUT" >&2

while true; do
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  # All pods across namespaces matching the elastic-blast selector.
  pods_json="$(kubectl get pods -A -l "$SELECTOR" -o json 2>/dev/null || echo '{"items":[]}')"
  echo "$pods_json" | python3 -c "
import sys,json
d=json.load(sys.stdin)
items=d.get('items',[])
phases={}
running_containers=0
running_pods=0
jobs=set()
running_jobs=set()
for p in items:
    ph=p.get('status',{}).get('phase','Unknown')
    phases[ph]=phases.get(ph,0)+1
    labels=p.get('metadata',{}).get('labels',{}) or {}
    jid=labels.get('elb-job-id')
    if jid:
        jobs.add(jid)
    if ph=='Running':
        running_pods+=1
        if jid:
            running_jobs.add(jid)
    for cs in p.get('status',{}).get('containerStatuses',[]) or []:
        if (cs.get('state') or {}).get('running'):
            running_containers+=1
print(json.dumps({'ts':'$ts','total_pods':len(items),'phases':phases,
    'running_pods':running_pods,'running_containers':running_containers,
    'distinct_jobs':len(jobs),'running_jobs':len(running_jobs)}))
" >> "$OUT"
  sleep "$INTERVAL"
done
