#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: scripts/dev/aks-equivalence-runner.sh <up|readiness|shell|exec|collect|job|job-file|job-status|job-logs|job-collect|job-down|down|status> [-- command...]

Creates a temporary AKS runner pod on the system node pool so Web BLAST
equivalence validation can be orchestrated from inside the cluster instead of
from a slow local workstation.

Commands:
  up          Create/update namespace, RBAC, runner script ConfigMap, and pod.
  readiness  Run a lightweight AKS readiness/evidence probe inside the pod.
  shell       Open an interactive shell in the runner pod.
  exec -- ... Run an arbitrary command in the runner pod.
  collect     Copy /workspace/evidence from the runner pod into docs/temp/.
  job -- ...  Start a detached system-node Kubernetes Job that survives local disconnects.
  job-file SCRIPT [-- args...]
              Start a detached system-node Kubernetes Job from a local script file.
  job-status  Show detached Job and Pod status. Defaults to latest runner-managed Job.
  job-logs    Show logs for a detached Job. Defaults to latest runner-managed Job.
  job-collect Copy /workspace/evidence from a detached Job Pod into docs/temp/.
  job-down    Delete a detached Job and its command ConfigMap.
  status      Show runner pod and matching system/blast nodes.
  down        Delete the runner pod/RBAC/namespace resources created by this script.

Environment:
  RUNNER_NAMESPACE      Default: elb-equivalence
  RUNNER_NAME           Default: elb-equivalence-runner
  RUNNER_IMAGE          Default: terminal sidecar image from ca-elb-control, if az can read it.
  CONTAINERAPP_RG       Default: rg-elb-dashboard
  CONTAINERAPP_NAME     Default: ca-elb-control
  SYSTEM_NODE_SELECTOR  Default: kubernetes.azure.com/mode=system
  BLAST_NODE_SELECTOR   Default: workload=blast
  LOCAL_EVIDENCE_DIR    Default: docs/temp/web-blast-equivalence/aks-runner-<timestamp>

Examples:
  scripts/dev/aks-equivalence-runner.sh up
  scripts/dev/aks-equivalence-runner.sh readiness
  scripts/dev/aks-equivalence-runner.sh exec -- kubectl get nodes -o wide
  scripts/dev/aks-equivalence-runner.sh job -- /runner/readiness.sh
  scripts/dev/aks-equivalence-runner.sh job-file scripts/dev/my-long-probe.sh -- arg1 arg2
  scripts/dev/aks-equivalence-runner.sh job-status
  scripts/dev/aks-equivalence-runner.sh job-collect
  scripts/dev/aks-equivalence-runner.sh collect
  scripts/dev/aks-equivalence-runner.sh down
USAGE
}

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
RUNNER_NAMESPACE=${RUNNER_NAMESPACE:-elb-equivalence}
RUNNER_NAME=${RUNNER_NAME:-elb-equivalence-runner}
CONTAINERAPP_RG=${CONTAINERAPP_RG:-rg-elb-dashboard}
CONTAINERAPP_NAME=${CONTAINERAPP_NAME:-ca-elb-control}
SYSTEM_NODE_SELECTOR=${SYSTEM_NODE_SELECTOR:-kubernetes.azure.com/mode=system}
BLAST_NODE_SELECTOR=${BLAST_NODE_SELECTOR:-workload=blast}
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
JOB_TIMESTAMP=$(date -u +%Y%m%d%H%M%S)
LOCAL_EVIDENCE_DIR=${LOCAL_EVIDENCE_DIR:-$PROJECT_ROOT/docs/temp/web-blast-equivalence/aks-runner-$TIMESTAMP}
RUNNER_JOB_NAME=${RUNNER_JOB_NAME:-elb-equivalence-job-$JOB_TIMESTAMP}

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: $1" >&2
    exit 1
  }
}

wait_for_runner_ready() {
  local deadline status ready
  deadline=$((SECONDS + 300))
  while (( SECONDS < deadline )); do
    status=$(kubectl -n "$RUNNER_NAMESPACE" get pod "$RUNNER_NAME" -o jsonpath='{.status.phase}' 2>/dev/null || true)
    ready=$(kubectl -n "$RUNNER_NAMESPACE" get pod "$RUNNER_NAME" -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null || true)
    if [[ "$status" == "Running" && "$ready" == "true" ]]; then
      return 0
    fi
    if [[ "$status" == "Failed" || "$status" == "Succeeded" ]]; then
      echo "ERROR: runner pod reached terminal phase: $status" >&2
      kubectl -n "$RUNNER_NAMESPACE" describe pod "$RUNNER_NAME" >&2 || true
      return 1
    fi
    sleep 5
  done
  echo "ERROR: timed out waiting for runner pod to become Ready" >&2
  kubectl -n "$RUNNER_NAMESPACE" get pod "$RUNNER_NAME" -o wide >&2 || true
  kubectl -n "$RUNNER_NAMESPACE" describe pod "$RUNNER_NAME" >&2 || true
  return 1
}

runner_image() {
  if [[ -n "${RUNNER_IMAGE:-}" ]]; then
    printf '%s\n' "$RUNNER_IMAGE"
    return
  fi
  if command -v az >/dev/null 2>&1; then
    local image
    image=$(az containerapp show \
      --resource-group "$CONTAINERAPP_RG" \
      --name "$CONTAINERAPP_NAME" \
      --query "properties.template.containers[?name=='terminal'].image | [0]" \
      -o tsv 2>/dev/null || true)
    if [[ -n "$image" && "$image" != "None" ]]; then
      printf '%s\n' "$image"
      return
    fi
  fi
  echo "ERROR: RUNNER_IMAGE is not set and terminal sidecar image could not be discovered" >&2
  echo "Set RUNNER_IMAGE=<acr-login-server>/elb-terminal:<tag> and retry." >&2
  exit 1
}

selector_key() {
  local selector=$1
  printf '%s' "${selector%%=*}"
}

selector_value() {
  local selector=$1
  if [[ "$selector" != *=* ]]; then
    echo "ERROR: selector must be key=value: $selector" >&2
    exit 2
  fi
  printf '%s' "${selector#*=}"
}

apply_runner() {
  need kubectl
  local image system_key system_value
  image=$(runner_image)
  system_key=$(selector_key "$SYSTEM_NODE_SELECTOR")
  system_value=$(selector_value "$SYSTEM_NODE_SELECTOR")

  kubectl get nodes -l "$SYSTEM_NODE_SELECTOR" >/dev/null

  apply_base_resources

  cat <<YAML | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: ${RUNNER_NAME}
  namespace: ${RUNNER_NAMESPACE}
  labels:
    app.kubernetes.io/name: ${RUNNER_NAME}
    app.kubernetes.io/managed-by: aks-equivalence-runner
spec:
  serviceAccountName: ${RUNNER_NAME}
  restartPolicy: Never
  nodeSelector:
    ${system_key}: "${system_value}"
  tolerations:
    - key: "CriticalAddonsOnly"
      operator: "Exists"
    - key: "kubernetes.azure.com/scalesetpriority"
      operator: "Exists"
      effect: "NoSchedule"
  containers:
    - name: runner
      image: ${image}
      imagePullPolicy: IfNotPresent
      command: ["/bin/bash", "-lc", "trap 'exit 0' TERM INT; while true; do sleep 3600; done"]
      env:
        - name: NODE_NAME
          valueFrom:
            fieldRef:
              fieldPath: spec.nodeName
        - name: BLASTDB
          value: /blast/blastdb
      resources:
        requests:
          cpu: 100m
          memory: 256Mi
        limits:
          cpu: "1"
          memory: 2Gi
      volumeMounts:
        - name: workspace
          mountPath: /workspace
        - name: runner-scripts
          mountPath: /runner
  volumes:
    - name: workspace
      emptyDir: {}
    - name: runner-scripts
      configMap:
        name: ${RUNNER_NAME}-scripts
        defaultMode: 0755
YAML

  wait_for_runner_ready
  kubectl -n "$RUNNER_NAMESPACE" get pod "$RUNNER_NAME" -o wide
}

apply_base_resources() {
  need kubectl
  kubectl create namespace "$RUNNER_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

  cat <<YAML | kubectl apply -f -
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${RUNNER_NAME}
  namespace: ${RUNNER_NAMESPACE}
  labels:
    app.kubernetes.io/name: ${RUNNER_NAME}
    app.kubernetes.io/managed-by: aks-equivalence-runner
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ${RUNNER_NAME}
  labels:
    app.kubernetes.io/name: ${RUNNER_NAME}
    app.kubernetes.io/managed-by: aks-equivalence-runner
rules:
  - apiGroups: [""]
    resources: ["nodes", "namespaces"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["pods", "pods/log", "configmaps", "events"]
    verbs: ["get", "list", "watch", "create", "delete", "patch", "update"]
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["get", "list", "watch", "create", "delete", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: ${RUNNER_NAME}
  labels:
    app.kubernetes.io/name: ${RUNNER_NAME}
    app.kubernetes.io/managed-by: aks-equivalence-runner
subjects:
  - kind: ServiceAccount
    name: ${RUNNER_NAME}
    namespace: ${RUNNER_NAMESPACE}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: ${RUNNER_NAME}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${RUNNER_NAME}-scripts
  namespace: ${RUNNER_NAMESPACE}
  labels:
    app.kubernetes.io/name: ${RUNNER_NAME}
    app.kubernetes.io/managed-by: aks-equivalence-runner
data:
  readiness.sh: |
    #!/usr/bin/env bash
    set -euo pipefail
    stamp=\$(date -u +%Y%m%dT%H%M%SZ)
    evidence_dir="/workspace/evidence/readiness-\$stamp"
    mkdir -p "\$evidence_dir"
    echo "EVIDENCE_DIR=\$evidence_dir" | tee "\$evidence_dir/summary.env"
    echo "RUNNER_NODE=\${NODE_NAME:-unknown}" | tee -a "\$evidence_dir/summary.env"
    echo "SYSTEM_NODE_SELECTOR=${SYSTEM_NODE_SELECTOR}" | tee -a "\$evidence_dir/summary.env"
    echo "BLAST_NODE_SELECTOR=${BLAST_NODE_SELECTOR}" | tee -a "\$evidence_dir/summary.env"
    kubectl version --client=true -o yaml | tee "\$evidence_dir/kubectl-client.yaml"
    if command -v blastn >/dev/null 2>&1; then
      blastn -version | tee "\$evidence_dir/blastn-version.txt"
    else
      echo "blastn not found" | tee "\$evidence_dir/blastn-version.txt"
    fi
    kubectl get nodes -o wide | tee "\$evidence_dir/nodes-wide.txt"
    kubectl get nodes -l "${SYSTEM_NODE_SELECTOR}" -o wide | tee "\$evidence_dir/system-nodes-wide.txt"
    kubectl get nodes -l "${BLAST_NODE_SELECTOR}" -o wide | tee "\$evidence_dir/blast-nodes-wide.txt"
    kubectl get pods,jobs -A -o wide | tee "\$evidence_dir/workloads-wide.txt"
    kubectl get pods,jobs -A -o wide | grep -Ei 'blast|warmup|elastic|elb' | tee "\$evidence_dir/blast-workloads.txt" || true
    python3 - <<'PY' "\$evidence_dir/summary.json"
    import json, os, pathlib, subprocess, sys
    out = pathlib.Path(sys.argv[1])
    def count_nodes(selector: str) -> int:
        p = subprocess.run(["kubectl", "get", "nodes", "-l", selector, "-o", "name"], text=True, stdout=subprocess.PIPE, check=True)
        return len([line for line in p.stdout.splitlines() if line.strip()])
    payload = {
        "runner_node": os.environ.get("NODE_NAME", "unknown"),
        "system_node_selector": "${SYSTEM_NODE_SELECTOR}",
        "blast_node_selector": "${BLAST_NODE_SELECTOR}",
        "system_nodes": count_nodes("${SYSTEM_NODE_SELECTOR}"),
        "blast_nodes": count_nodes("${BLAST_NODE_SELECTOR}"),
    }
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    PY
YAML
}

write_job_script() {
  local script_path=$1
  shift
  {
    cat <<'SCRIPT'
#!/usr/bin/env bash
set -Eeuo pipefail
stamp=$(date -u +%Y%m%dT%H%M%SZ)
evidence_dir="/workspace/evidence/${RUNNER_JOB_NAME:-detached-job}-$stamp"
mkdir -p "$evidence_dir"
exec > >(tee -a "$evidence_dir/job.log") 2>&1
echo "STARTED_AT=$stamp" | tee "$evidence_dir/summary.env"
echo "RUNNER_JOB_NAME=${RUNNER_JOB_NAME:-unknown}" | tee -a "$evidence_dir/summary.env"
echo "RUNNER_NODE=${NODE_NAME:-unknown}" | tee -a "$evidence_dir/summary.env"
echo "SYSTEM_NODE_SELECTOR=${SYSTEM_NODE_SELECTOR:-unknown}" | tee -a "$evidence_dir/summary.env"
echo "BLAST_NODE_SELECTOR=${BLAST_NODE_SELECTOR:-unknown}" | tee -a "$evidence_dir/summary.env"
kubectl get nodes -o wide | tee "$evidence_dir/nodes-wide.txt"
kubectl get nodes -l "${SYSTEM_NODE_SELECTOR:-kubernetes.azure.com/mode=system}" -o wide | tee "$evidence_dir/system-nodes-wide.txt"
kubectl get nodes -l "${BLAST_NODE_SELECTOR:-workload=blast}" -o wide | tee "$evidence_dir/blast-nodes-wide.txt"
if command -v blastn >/dev/null 2>&1; then
  blastn -version | tee "$evidence_dir/blastn-version.txt"
fi
cd /workspace
echo "COMMAND_BEGIN"
set +e
SCRIPT
    printf '  '
    printf '%q ' "$@"
    printf '\n'
    cat <<'SCRIPT'
exit_code=$?
set -e
finished_at=$(date -u +%Y%m%dT%H%M%SZ)
echo "COMMAND_EXIT_CODE=$exit_code" | tee -a "$evidence_dir/summary.env"
echo "FINISHED_AT=$finished_at" | tee -a "$evidence_dir/summary.env"
printf '{"job_name":"%s","runner_node":"%s","started_at":"%s","finished_at":"%s","exit_code":%s}\n' \
  "${RUNNER_JOB_NAME:-unknown}" "${NODE_NAME:-unknown}" "$stamp" "$finished_at" "$exit_code" > "$evidence_dir/summary.json"
echo "COMMAND_END exit_code=$exit_code"
touch "$evidence_dir/COLLECT_READY"
if [[ "${HOLD_FOR_COLLECT:-1}" == "1" ]]; then
  echo "HOLD_FOR_COLLECT=1; evidence is ready under $evidence_dir"
  echo "Delete this Job after collection."
  while true; do sleep 3600; done
fi
exit "$exit_code"
SCRIPT
  } > "$script_path"
}

latest_job_name() {
  kubectl -n "$RUNNER_NAMESPACE" get jobs \
    -l app.kubernetes.io/managed-by=aks-equivalence-runner,app.kubernetes.io/component=detached-job \
    --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null || true
}

job_pod_name() {
  local job_name=$1
  kubectl -n "$RUNNER_NAMESPACE" get pods -l "job-name=$job_name" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true
}

resolve_job_name() {
  local job_name=${1:-}
  if [[ -z "$job_name" ]]; then
    job_name=$(latest_job_name)
  fi
  if [[ -z "$job_name" ]]; then
    echo "ERROR: no detached runner Job found" >&2
    exit 2
  fi
  printf '%s\n' "$job_name"
}

start_detached_job() {
  need kubectl
  local image system_key system_value script_tmp job_name
  if [[ ${1:-} == "--" ]]; then
    shift
  fi
  if [[ $# -eq 0 ]]; then
    echo "ERROR: job requires a command after --" >&2
    exit 2
  fi
  image=$(runner_image)
  system_key=$(selector_key "$SYSTEM_NODE_SELECTOR")
  system_value=$(selector_value "$SYSTEM_NODE_SELECTOR")
  job_name=$RUNNER_JOB_NAME
  script_tmp=$(mktemp)

  kubectl get nodes -l "$SYSTEM_NODE_SELECTOR" >/dev/null
  apply_base_resources
  write_job_script "$script_tmp" "$@"
  kubectl -n "$RUNNER_NAMESPACE" create configmap "$job_name-script" \
    --from-file=job.sh="$script_tmp" \
    --dry-run=client -o yaml | kubectl apply -f -
  if [[ -n "${JOB_USER_SCRIPT_PATH:-}" ]]; then
    kubectl -n "$RUNNER_NAMESPACE" create configmap "$job_name-user-script" \
      --from-file=user.sh="$JOB_USER_SCRIPT_PATH" \
      --dry-run=client -o yaml | kubectl apply -f -
  fi

  cat <<YAML | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job_name}
  namespace: ${RUNNER_NAMESPACE}
  labels:
    app.kubernetes.io/name: ${RUNNER_NAME}
    app.kubernetes.io/component: detached-job
    app.kubernetes.io/managed-by: aks-equivalence-runner
spec:
  backoffLimit: 0
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ${RUNNER_NAME}
        app.kubernetes.io/component: detached-job
        app.kubernetes.io/managed-by: aks-equivalence-runner
    spec:
      serviceAccountName: ${RUNNER_NAME}
      restartPolicy: Never
      nodeSelector:
        ${system_key}: "${system_value}"
      tolerations:
        - key: "CriticalAddonsOnly"
          operator: "Exists"
        - key: "kubernetes.azure.com/scalesetpriority"
          operator: "Exists"
          effect: "NoSchedule"
      containers:
        - name: runner
          image: ${image}
          imagePullPolicy: IfNotPresent
          command: ["/bin/bash", "/runner-job/job.sh"]
          env:
            - name: RUNNER_JOB_NAME
              value: ${job_name}
            - name: SYSTEM_NODE_SELECTOR
              value: ${SYSTEM_NODE_SELECTOR}
            - name: BLAST_NODE_SELECTOR
              value: ${BLAST_NODE_SELECTOR}
            - name: HOLD_FOR_COLLECT
              value: "1"
            - name: NODE_NAME
              valueFrom:
                fieldRef:
                  fieldPath: spec.nodeName
            - name: BLASTDB
              value: /blast/blastdb
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              cpu: "1"
              memory: 2Gi
          volumeMounts:
            - name: workspace
              mountPath: /workspace
            - name: runner-scripts
              mountPath: /runner
            - name: job-script
              mountPath: /runner-job
            - name: user-script
              mountPath: /runner-user
      volumes:
        - name: workspace
          emptyDir: {}
        - name: runner-scripts
          configMap:
            name: ${RUNNER_NAME}-scripts
            defaultMode: 0755
        - name: job-script
          configMap:
            name: ${job_name}-script
            defaultMode: 0755
        - name: user-script
          configMap:
            name: ${job_name}-user-script
            defaultMode: 0755
            optional: true
YAML

  printf 'Started detached Job %s in namespace %s\n' "$job_name" "$RUNNER_NAMESPACE"
  printf 'Check it with: %s job-status %s\n' "$0" "$job_name"
  printf 'Collect evidence later with: %s job-collect %s\n' "$0" "$job_name"
  rm -f "$script_tmp"
}

start_detached_job_file() {
  local script_path
  if [[ $# -lt 1 ]]; then
    echo "ERROR: job-file requires a local script path" >&2
    exit 2
  fi
  script_path=$1
  shift
  if [[ ${1:-} == "--" ]]; then
    shift
  fi
  if [[ ! -f "$script_path" ]]; then
    echo "ERROR: script file not found: $script_path" >&2
    exit 2
  fi
  JOB_USER_SCRIPT_PATH=$script_path start_detached_job -- /runner-user/user.sh "$@"
}

show_job_status() {
  need kubectl
  local job_name
  job_name=$(resolve_job_name "${1:-}")
  kubectl -n "$RUNNER_NAMESPACE" get job "$job_name" -o wide || true
  kubectl -n "$RUNNER_NAMESPACE" get pods -l "job-name=$job_name" -o wide || true
}

show_job_logs() {
  need kubectl
  local job_name
  job_name=$(resolve_job_name "${1:-}")
  kubectl -n "$RUNNER_NAMESPACE" logs "job/$job_name" --tail="${TAIL_LINES:-200}"
}

collect_job_evidence() {
  need kubectl
  local job_name pod_name target_dir
  job_name=$(resolve_job_name "${1:-}")
  pod_name=$(job_pod_name "$job_name")
  if [[ -z "$pod_name" ]]; then
    echo "ERROR: no pod found for Job $job_name" >&2
    exit 2
  fi
  target_dir=${LOCAL_EVIDENCE_DIR:-$PROJECT_ROOT/docs/temp/web-blast-equivalence/aks-runner-$TIMESTAMP}
  target_dir="$target_dir/$job_name"
  mkdir -p "$target_dir"
  kubectl -n "$RUNNER_NAMESPACE" cp "$pod_name:/workspace/evidence" "$target_dir"
  printf 'Detached Job evidence copied to %s\n' "$target_dir"
}

delete_detached_job() {
  need kubectl
  local job_name
  job_name=$(resolve_job_name "${1:-}")
  kubectl -n "$RUNNER_NAMESPACE" delete job "$job_name" --ignore-not-found=true
  kubectl -n "$RUNNER_NAMESPACE" delete configmap "$job_name-script" --ignore-not-found=true
  kubectl -n "$RUNNER_NAMESPACE" delete configmap "$job_name-user-script" --ignore-not-found=true
}

run_readiness() {
  need kubectl
  kubectl -n "$RUNNER_NAMESPACE" exec "$RUNNER_NAME" -- /runner/readiness.sh
}

open_shell() {
  need kubectl
  kubectl -n "$RUNNER_NAMESPACE" exec -it "$RUNNER_NAME" -- /bin/bash
}

exec_runner() {
  need kubectl
  if [[ ${1:-} == "--" ]]; then
    shift
  fi
  if [[ $# -eq 0 ]]; then
    echo "ERROR: exec requires a command after --" >&2
    exit 2
  fi
  kubectl -n "$RUNNER_NAMESPACE" exec "$RUNNER_NAME" -- "$@"
}

collect_evidence() {
  need kubectl
  mkdir -p "$LOCAL_EVIDENCE_DIR"
  kubectl -n "$RUNNER_NAMESPACE" cp "$RUNNER_NAME:/workspace/evidence" "$LOCAL_EVIDENCE_DIR"
  printf 'Evidence copied to %s\n' "$LOCAL_EVIDENCE_DIR"
}

show_status() {
  need kubectl
  kubectl -n "$RUNNER_NAMESPACE" get pod "$RUNNER_NAME" -o wide || true
  kubectl get nodes -l "$SYSTEM_NODE_SELECTOR" -o wide
  kubectl get nodes -l "$BLAST_NODE_SELECTOR" -o wide
}

delete_runner() {
  need kubectl
  kubectl -n "$RUNNER_NAMESPACE" delete pod "$RUNNER_NAME" --ignore-not-found=true
  kubectl -n "$RUNNER_NAMESPACE" delete configmap "${RUNNER_NAME}-scripts" --ignore-not-found=true
  kubectl delete clusterrolebinding "$RUNNER_NAME" --ignore-not-found=true
  kubectl delete clusterrole "$RUNNER_NAME" --ignore-not-found=true
  kubectl -n "$RUNNER_NAMESPACE" delete serviceaccount "$RUNNER_NAME" --ignore-not-found=true
  if [[ "${DELETE_RUNNER_NAMESPACE:-0}" == "1" ]]; then
    kubectl delete namespace "$RUNNER_NAMESPACE" --ignore-not-found=true
  fi
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

cmd=$1
shift
case "$cmd" in
  up) apply_runner "$@" ;;
  readiness) run_readiness "$@" ;;
  shell) open_shell "$@" ;;
  exec) exec_runner "$@" ;;
  collect) collect_evidence "$@" ;;
  job) start_detached_job "$@" ;;
  job-file) start_detached_job_file "$@" ;;
  job-status) show_job_status "$@" ;;
  job-logs) show_job_logs "$@" ;;
  job-collect) collect_job_evidence "$@" ;;
  job-down) delete_detached_job "$@" ;;
  status) show_status "$@" ;;
  down) delete_runner "$@" ;;
  -h|--help|help) usage ;;
  *)
    echo "ERROR: unknown command: $cmd" >&2
    usage
    exit 2
    ;;
esac
