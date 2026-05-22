# 2026-05-22 — `local-debug-auth.sh` one-shot toggle for real MSAL login locally

## Motivation

Three things have to flip together for the local dashboard to render as the
real `az login` identity instead of the synthetic `anonymous` dev-bypass
caller:

1. Storage RBAC (`Storage Blob/Table Data Contributor`, `Storage Account
   Contributor`, `Reader` on the workload RG, `AcrPull` on the workload ACR).
2. Storage network surface — must be reachable from the laptop
   (production posture is `publicNetworkAccess: Disabled`).
3. `AUTH_DEV_BYPASS=false` in root `.env`, `VITE_AUTH_DEV_BYPASS=false` in
   `web/.env.local`, `API_CLIENT_ID` exported so the api can validate the
   bearer's audience, and a restart of `api` + `vite`.

Before this change, doing all three required calling `grant-local-rbac.sh`,
`storage-public-access.sh on`, hand-editing two env files, and restarting
two background processes — easy to forget a step, and harder still to roll
back cleanly afterwards (charter §9 requires `publicNetworkAccess` back to
`Disabled` after debugging).

## User-facing change

New helper `scripts/dev/local-debug-auth.sh` with three subcommands:

```bash
scripts/dev/local-debug-auth.sh on       # ensure RBAC + open storage + bypass=false + restart
scripts/dev/local-debug-auth.sh off      # bypass=true + close storage + restart  (RBAC kept)
scripts/dev/local-debug-auth.sh status   # print current state without mutating
```

Wired into `local-run.sh` as `auth-on` / `auth-off` / `auth-status` for
consistency with the existing `storage-on` / `storage-off` / `storage-status`
subcommands.

The script is idempotent (safe to re-run), auto-detects the workload
Storage account / ACR / `API_CLIENT_ID` from `azd env get-values`, and
pre-checks `az role assignment list` at the storage scope so it fails fast
with a clear message when the caller lacks `User Access Administrator` /
`Owner`. Override defaults with `--storage`, `--storage-rg`, `--acr`,
`--acr-rg`, `--subscription`; skip individual steps with `--skip-rbac`,
`--skip-storage`, `--skip-restart`; keep storage open on `off` with
`--no-close-storage`.

## API / IaC diff summary

* Added [scripts/dev/local-debug-auth.sh](../../../scripts/dev/local-debug-auth.sh) (~320 lines).
* Added `auth-on` / `auth-off` / `auth-status` dispatch in
  [scripts/dev/local-run.sh](../../../scripts/dev/local-run.sh); extended
  its usage banner.
* Extended `load_local_azure_env` in `local-run.sh` to pass through
  `API_CLIENT_ID`, `AUTH_DEV_BYPASS`, `VITE_AUTH_DEV_BYPASS` from `.env`
  so the new bypass toggles survive a shell restart.
* Added a new "Local debug as your real az-login identity (one-shot)"
  section to [docs/troubleshooting.md](../../troubleshooting.md).
* Charter [.github/copilot-instructions.md §9](../../../.github/copilot-instructions.md)
  now documents the toggle as the canonical local-debug session entry
  point next to the existing `storage-public-access.sh` callout.
* [AGENTS.md](../../../AGENTS.md) tripwire #8 lists both helpers (storage
  network toggle + auth session toggle); the Validation cheatsheet gained
  a "Local debug as real az identity" row so future agents discover the
  enable / disable flow without searching.
* No Azure SDK / route changes. No production code path touched.

## Validation evidence

Performed against the live `stelbdashboard01mul5oh5j` deployment in
`rg-elb-dashboard-01`:

```text
$ bash scripts/dev/local-debug-auth.sh status \
    --storage stelbdashboard01mul5oh5j --storage-rg rg-elb-dashboard-01
── local-debug-auth status ─────────────────────────────
  signed-in user:   admin@MngEnvMCAP982529.onmicrosoft.com (25e0aef5-…)
  subscription:     577d6332-de48-4a30-be66-dded26a712ea
  storage:          stelbdashboard01mul5oh5j (rg: rg-elb-dashboard-01)
  AUTH_DEV_BYPASS:  false  (root .env)
  VITE_AUTH_DEV_BYPASS: false  (web/.env.local)
  storage network:  {"default":"Allow","public":"Enabled"}
  RBAC at storage scope:
    - Storage Blob Data Contributor
  api (8085):       pid 347661
  vite (8090):      pid 348089
```

```text
$ bash scripts/dev/local-debug-auth.sh on --storage … --storage-rg … --skip-restart
[…] Step 1/4 — RBAC (grant-local-rbac.sh)
  [skip] Storage Blob Data Contributor already assigned at …
  [ok  ] granted Storage Table Data Contributor at …
  [ok  ] granted Storage Account Contributor at …
  [ok  ] granted Reader at …rg-elb-dashboard-01
[…] Step 2/4 — Storage network
  storage already publicNetworkAccess=Enabled / defaultAction=Allow — no change
[…] Step 3/4 — Env files
  .env ← AUTH_DEV_BYPASS=false, VITE_AUTH_DEV_BYPASS=false, API_CLIENT_ID=ddf48…
  web/.env.local ← VITE_AUTH_DEV_BYPASS=false
```

Idempotent re-run skipped the already-assigned Blob Data Contributor and
detected the already-open storage state; env files contain no duplicate
keys after the upsert (`grep AUTH_DEV_BYPASS .env` → single line).

`bash -n` syntax-check passes for both `local-debug-auth.sh` and the
updated `local-run.sh`.
