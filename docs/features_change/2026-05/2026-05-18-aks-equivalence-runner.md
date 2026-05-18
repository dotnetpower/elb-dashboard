# AKS Equivalence Runner

## Motivation

Local workstation performance is too low for Web BLAST equivalence validation. The validation workflow needs a lightweight way to run orchestration and evidence collection from inside AKS while keeping heavy BLAST work on the existing blast node pool.

## User-facing change

Added a dev runner script that creates a temporary runner pod on the AKS system node pool. The pod can run readiness probes, execute cluster-side commands, and copy evidence back into `docs/temp/web-blast-equivalence/`.

The script also supports detached system-node Kubernetes Jobs via `job -- ...`. Detached Jobs run independently after creation, so long equivalence orchestration continues even if the local workstation loses network connectivity. The Job keeps its pod alive after command completion so `job-collect` can copy `/workspace/evidence` before `job-down` deletes the temporary resources.

## API/IaC diff summary

No API or product IaC changes. Added `scripts/dev/aks-equivalence-runner.sh`, which creates temporary Kubernetes namespace/RBAC/ConfigMap/Pod/Job resources using `kubectl apply`. Interactive runner resources are cleaned up with `down`; detached Jobs are cleaned up with `job-down` after evidence collection.

## Validation evidence

- `bash -n scripts/dev/aks-equivalence-runner.sh` passed.
- `scripts/dev/aks-equivalence-runner.sh up` created the temporary namespace/RBAC/ConfigMap/Pod and placed the runner on `aks-systempool-41800479-vmss000004`.
- The first pod attempt exposed missing AKS kubelet `AcrPull`; granting `AcrPull` on `acrelbnm5virmqrdi5c` to kubelet object id `a820e022-7f72-4438-8e10-aef608912754` allowed the terminal image pull.
- `scripts/dev/aks-equivalence-runner.sh readiness` generated cluster-side evidence; `collect` copied it to `docs/temp/web-blast-equivalence/aks-runner-20260518T042303Z/`.
- Readiness summary: `system_nodes=1`, `blast_nodes=10`, runner node `aks-systempool-41800479-vmss000004`, BLAST+ `2.17.0+`.
- `scripts/dev/aks-equivalence-runner.sh exec -- bash -lc 'echo runner:$NODE_NAME; kubectl get nodes -l workload=blast -o name | wc -l'` returned the system runner node and `10` blast nodes.
- `scripts/dev/aks-equivalence-runner.sh job -- /runner/readiness.sh` created detached Job `elb-equivalence-job-20260518050056` on `aks-systempool-41800479-vmss000004`.
- `scripts/dev/aks-equivalence-runner.sh job-status elb-equivalence-job-20260518050056` showed the pod `Running` after command completion because it is intentionally held for collection.
- `scripts/dev/aks-equivalence-runner.sh job-collect elb-equivalence-job-20260518050056` copied evidence to `docs/temp/web-blast-equivalence/aks-runner-20260518T050110Z/elb-equivalence-job-20260518050056/`.
- Detached Job summary reports `COMMAND_EXIT_CODE=0`, runner node `aks-systempool-41800479-vmss000004`, `system_nodes=1`, and `blast_nodes=10`.
