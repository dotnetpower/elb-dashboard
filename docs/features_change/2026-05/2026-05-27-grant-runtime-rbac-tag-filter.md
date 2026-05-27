# grant-runtime-rbac.sh — tag-based AKS auto-detect

## Motivation

In a subscription that hosts multiple AKS clusters from different apps,
`scripts/dev/grant-runtime-rbac.sh` refused to guess and exited 3 with
"multiple AKS clusters found in subscription — be explicit with
`--cluster-rg`". `cli-upgrade.sh` caught the non-zero exit and emitted
the misleading `WARN: runtime RBAC grant did not fully succeed` block,
which suggested the operator needed a tenant/sub admin even though the
dashboard's own tag contract was enough to disambiguate.

Reproducer: a subscription with `aks-bori-dev`, `aks-kbtest`, `elb-v3-b2`,
`elastic-blast-moonchoi-01`, `elb-cluster-01` — only the last one carries
`managedBy=elb-dashboard`.

## User-facing change

`scripts/dev/grant-runtime-rbac.sh` now resolves the AKS cluster RG in
this order:

1. `--cluster-rg` flag (explicit operator intent).
2. `$ELB_CLUSTER_RG_NAME` from the shell or `azd env get-value` — same
   variable `deploy.sh` already treats as the canonical pointer.
3. `az aks list` filtered by the `managedBy=elb-dashboard` tag the
   dashboard always stamps on clusters it provisions
   (`api/tasks/azure/cluster_params.py`), narrowed further by
   `azd-env-name=$AZURE_ENV_NAME` when available. Refuses only if the
   filter still leaves more than one candidate.
4. Distinguishes "zero AKS in subscription" (bootstrap hint) from
   "zero tag-matching AKS but other clusters exist" (lists the other
   clusters and asks the operator to pass `--cluster-rg` /
   `ELB_CLUSTER_RG_NAME`).

In the multi-AKS reproducer above the script now prints:

```
[..] [auto] picked cluster RG 'rg-elb-cluster' (managedBy=elb-dashboard, azd-env-name=elb-dashboard)
```

…and the `cli-upgrade.sh` preflight succeeds without the spurious WARN
block.

## API / IaC diff summary

* `scripts/dev/grant-runtime-rbac.sh`
  * Always-on `azd env get-values` pull for `AZURE_ENV_NAME` and
    `ELB_CLUSTER_RG_NAME` (previously only ran when
    `CONTAINER_APP_NAME` / `AZURE_RESOURCE_GROUP` were missing).
  * Cluster-RG resolution block now uses tag filter + azd-env filter
    via inline python.
  * Fixes the latent stdin/heredoc bug where `python3 - <<'PY' ... PY`
    would shadow the piped JSON — JSON is now passed via the
    `AKS_JSON` env var.
  * New "[auto] picked cluster RG …" log line and a refined
    "no tag-matching cluster" error message.

No other scripts, Bicep, or backend code paths change. The Container App
identity model and the role assignments themselves are unchanged.

## Validation

* `bash -n scripts/dev/grant-runtime-rbac.sh` — clean.
* Live dry-run against the reproducer subscription
  (`b052302c-…`, 5 AKS clusters, only `elb-cluster-01` carries the
  `managedBy=elb-dashboard` tag):

  ```
  $ bash scripts/dev/grant-runtime-rbac.sh \
      --container-app ca-elb-dashboard --rg rg-elb-dashboard --dry-run --yes
  [08:25:58]   [auto] picked cluster RG 'rg-elb-cluster' (managedBy=elb-dashboard, azd-env-name=elb-dashboard)
  ...
  AKS cluster RG:  rg-elb-cluster
  (dry-run — no role assignments will be created)
    [skip] Contributor already assigned at /…/rg-elb-cluster
    [skip] User Access Administrator already assigned at /…/rg-elb-cluster
  Summary: created=0 skipped=2 failed=0
  ```

* `bash scripts/dev/cli-upgrade.sh full --dry-run --yes --allow-dirty`
  reaches the "Dry-run complete" line with no `WARN: runtime RBAC grant
  did not fully succeed` block. (Dry-run still short-circuits before
  calling the script, as before; the real-deploy code path is exercised
  by the standalone invocation above.)
