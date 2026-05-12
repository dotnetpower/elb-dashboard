# Bundle remote-terminal cloud-init in deployment package

**Date**: 2026-05-12
**Scope**: `api/activities/terminal.py`, `api/scripts/cloud-init/remote-terminal.yaml` (new copy)

## Motivation

Provision Terminal failed at step 5 (Create VM) with:

```
FileNotFoundError: [Errno 2] No such file or directory:
'/home/site/scripts/cloud-init/remote-terminal.yaml'
```

The deployment package built from `api/` does not include the
sibling `scripts/` directory, but `activity_create_vm` resolved the
cloud-init path with `Path(__file__).parent.parent.parent`
(repo-root layout) and fell off the end of `wwwroot/` in production.

## User-facing change

Remote Terminal provisioning succeeds end-to-end. After
fixing the path resolver and bundling the YAML inside the deployment
package, the orchestrator now:

1. Resource Group ✓
2. Network & IP ✓ (PIP GET-reuse, DNS hash)
3. Key Vault ✓ (RBAC-mode tolerant — see `2026-05-12-keyvault-rbac-mode.md`)
4. Generate Password ✓
5. Create VM ✓ (cloud-init YAML now found)
6. Cloud Init ✓ (status `done` reported by the VM)

Verified live in `rg-elb-demo-terminal` with VM `vm-elb-terminal`,
FQDN `elb-term-vm-elb-terminal-013f5a.koreacentral.cloudapp.azure.com`.

## API / IaC diff summary

`api/activities/terminal.py`:

- New `_resolve_cloud_init_path()` helper. Looks for the YAML in:
  1. `<api_dir>/scripts/cloud-init/remote-terminal.yaml` (production,
     bundled inside the deployment zip).
  2. `<repo_root>/scripts/cloud-init/remote-terminal.yaml` (local
     dev where `scripts/` lives one level above `api/`).
  3. `CLOUD_INIT_PATH` environment variable override (escape hatch).
  Raises `FileNotFoundError` with the search path list when none
  exist.
- `activity_create_vm` calls `_resolve_cloud_init_path()` instead of
  the module-level constant. The constant remains for backward
  compatibility but is no longer used in the hot path.

`api/scripts/cloud-init/remote-terminal.yaml`:

- New file — verbatim copy of `scripts/cloud-init/remote-terminal.yaml`
  at the repo root. Maintained as a deployment-package mirror so
  `cd api && zip -r …` packages it. The repo-root copy stays as the
  source of truth for documentation and dev tooling.

## Validation evidence

- `pytest -q api/tests/` → 13 passed.
- Function App redeployed via `WEBSITE_RUN_FROM_PACKAGE` user-
  delegation SAS (`funcapp-cloudinit.zip`); restart + `/api/health`
  200.
- Triggered Provision Terminal from the UI (browser session, signed
  in). Watched the orchestrator march through Resource Group →
  Network → Key Vault → Password → VM → Cloud Init. No exceptions
  in App Insights for the entire run.
- Verified the connection card surfaces FQDN, username, password
  reveal, SSH command, and cloud-init status `done`.

## Operational note

Subscription Defender for Servers policy auto-installs the
`MDE.Linux` VM extension on every new VM. While MDE is in
`Creating`/`Updating` state (~5–10 min on D4s_v5), VM Run Command
calls return `(Conflict) Run command extension execution is in
progress`. The orchestrator's `check_cloud_init_activity` handles
that as a transient error (treats as `running` and retries) thanks
to `provision_terminal_orchestrator`'s existing exception
classification, so the only practical effect is a longer
"Cloud Init" phase. Today's run reached `done` at attempt 12 (≈6
min into the polling phase). The 30 × 30 s budget remains adequate.
