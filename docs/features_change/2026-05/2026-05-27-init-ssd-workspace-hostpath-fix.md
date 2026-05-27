# init-ssd Job stuck in CreateContainerConfigError ("stat /workspace: no such file or directory")

## Motivation

Submitted BLAST jobs were stalling with init-ssd pods in
`Waiting (CreateContainerConfigError)` and the message
`stat /workspace: no such file or directory`:

```
Containers:
  get-blastdb:
    State:  Waiting (CreateContainerConfigError)
      Message: stat /workspace: no such file or directory
  import-query-batches:
    State:  Waiting (CreateContainerConfigError)
      Message: stat /workspace: no such file or directory
```

Root cause: the init-ssd Job mounts `/workspace` as a `hostPath` volume. The
companion `create-workspace` DaemonSet (kube-system) is responsible for creating
and `chmod 777`-ing that directory on every node. Upstream ships the DaemonSet
with `nodeSelector: kubernetes.io/os: linux` but **no tolerations**, so on AKS
clusters where the blast pool carries the `workload=blast:NoSchedule` taint
the DaemonSet cannot land on the blast nodes. The init-ssd Job (which does
tolerate the taint and selects `workload: blast`) then gets scheduled on a
node where `/workspace` does not exist, and kubelet rejects the bind-mount.

## User-facing change

* New BLAST submissions on AKS no longer get stuck in
  `CreateContainerConfigError` waiting for `/workspace`.
* The dashboard's existing warmup card (which reads
  `daemonsets/create-workspace.status.numberReady` via
  [api/services/k8s/monitoring.py](../../api/services/k8s/monitoring.py))
  now reports a non-zero ready count once the blast pool comes up.

## API / IaC diff

* No API change.
* No Bicep change.
* Terminal sidecar build-time patch only —
  [terminal/patch_elastic_blast.py](../../terminal/patch_elastic_blast.py)
  gains `patch_create_workspace_daemonset_tolerations()` which injects

  ```yaml
        tolerations:
        - key: workload
          operator: Equal
          value: blast
          effect: NoSchedule
  ```

  immediately above the existing `nodeSelector: kubernetes.io/os: linux`
  block in both
  `job-init-local-ssd-aks.yaml.template` and
  `job-init-ssd-shard-aks.yaml.template`. The patch is idempotent (marker
  on the combined `tolerations` + `nodeSelector` block).

## Rollout

Because the patcher runs at terminal image build time
([terminal/Dockerfile](../../terminal/Dockerfile),
[terminal/Dockerfile.base](../../terminal/Dockerfile.base)), in-flight
clusters that were created from the previous (unpatched) template still need
manual cleanup:

```bash
# 1. Rebuild the terminal sidecar image and roll the Container App revision.
scripts/dev/postprovision.sh    # or quick-deploy.sh terminal

# 2. Remove the broken DaemonSet + stuck init-ssd Job so the next submit
#    re-applies the patched template.
kubectl --context <ctx> delete daemonset -n kube-system create-workspace --ignore-not-found
kubectl --context <ctx> delete job -l app=setup --ignore-not-found
```

The next BLAST submit recreates both with the toleration in place.

## Validation

* `uv run pytest -q api/tests/test_terminal_patch_elastic_blast.py` —
  7 passed (including the two new tests
  `test_patch_create_workspace_daemonset_tolerations_adds_blast_toleration`
  and `test_patch_create_workspace_daemonset_tolerations_is_idempotent`).
* `uv run ruff check terminal/patch_elastic_blast.py
  api/tests/test_terminal_patch_elastic_blast.py` — clean.
* Dry-run applied the patch against the real upstream clone at
  `~/dev/elastic-blast-azure` — both templates contain the expected
  toleration block, and a second run is a no-op (idempotency confirmed).
