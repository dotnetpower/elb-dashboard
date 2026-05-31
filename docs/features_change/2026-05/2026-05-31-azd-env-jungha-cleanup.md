# 2026-05-31 — azd default env / `.env` jungha leak cleanup

## Motivation

A prior `quick-deploy.sh all` run silently shipped to the **jungha** demo subscription
(`577d6332-de48-4a30-be66-dded26a712ea`, tenant `184be312-…`) instead of the operator's
real **moonchoi** subscription (`b052302c-4c8d-49a4-aa2f-9d60a7301a80`,
tenant `78716814-…`). Two failure surfaces combined:

1. The default `azd` environment `elb-dashboard` carried the jungha sub/tenant.
2. `.env` and `web/.env.local` carried jungha tenant / client App Registration values
   that `quick-deploy.sh` happily PATCHed into the cloud frontend via `--set-env-vars`.

Because both subscriptions host an identically-named `ca-elb-dashboard` Container App
inside an identically-named `rg-elb-dashboard` resource group, the only safe
discriminator is the Container Apps FQDN suffix (`ambitiousisland-50bfcf60.*` for
moonchoi, `blackplant-9a7765c4.*` for jungha) — the operator caught the wrong
target only after deploy validation.

## User-facing change

None at runtime. This change tightens local configuration so future invocations of
`scripts/dev/quick-deploy.sh` target the operator's real subscription by default and
no longer carry jungha-shaped MSAL values for any frontend `--set-env-vars` PATCH.

## API / IaC diff summary

- `azd env set AZURE_SUBSCRIPTION_ID b052302c-4c8d-49a4-aa2f-9d60a7301a80 -e elb-dashboard`
- `azd env set AZURE_TENANT_ID 78716814-cb3c-4b74-8fa8-0688dbd41ec3   -e elb-dashboard`
- Removed `.azure/elb-ca`, `.azure/elb-demo`, `.azure/elb-prod` — all carried
  jungha-shaped values (or in `elb-prod`'s case the wrong RG `rg-elb-prod`). They are
  recreatable in seconds via `azd env new` if a multi-environment story is needed.
  `.azure/` is `.gitignore`d so these deletions touch only the local workstation.
- `.env`: `API_CLIENT_ID=ddf48c19-…` (jungha App Reg) → `14cf2a04-9985-4372-aa68-8d54c9adb75a` (moonchoi App Reg).
- `web/.env.local`: `VITE_AZURE_TENANT_ID` → `78716814-…`, `VITE_AZURE_CLIENT_ID` → `14cf2a04-…`.
- `scripts/dev/quick-deploy.sh`: extended the `load_simple_env_file "$REPO_ROOT/web/.env.local"`
  skip-list to also ignore `VITE_AZURE_TENANT_ID`, `VITE_AZURE_CLIENT_ID`,
  `VITE_AZURE_REDIRECT_URI`, and `API_CLIENT_ID`. `web/.env.local` is by design a
  *local* dev override file (vite dev server, local-debug toggles, optionally a
  developer's personal MSAL App Reg) and must never feed values into a cloud deploy.
  Defense-in-depth on top of fixing the values themselves — any future developer
  putting their own MSAL config in `web/.env.local` will not silently corrupt a
  cloud frontend's `runtime-config.js`.

## Validation

- `azd env get-values -e elb-dashboard | grep AZURE_` → returns moonchoi sub + tenant
  + `rg-elb-dashboard` + `koreacentral`.
- `azd env list` → only `elb-dashboard` remains.
- `grep -r 184be312 web/.env.local .env` and `grep -r ddf48c19 web/.env.local .env` →
  no active matches (commented references in `.env` history block intentionally kept).
- Live deployed revision `ca-elb-dashboard--0000043` (the moonchoi target deployed
  earlier today) confirmed: `runtime-config.js` shows
  `VITE_AZURE_TENANT_ID:"78716814-cb3c-4b74-8fa8-0688dbd41ec3"`,
  `VITE_AZURE_CLIENT_ID:"14cf2a04-9985-4372-aa68-8d54c9adb75a"`,
  `VITE_API_BASE_URL:""`, `VITE_AUTH_DEV_BYPASS:"false"`; `/api/health` 200.
- `uv run ruff check scripts/dev/quick-deploy.sh` — N/A (bash, not python).
  Bash syntax verified by `bash -n scripts/dev/quick-deploy.sh`.

## Explicitly out of scope (operator follow-ups)

- The orphaned jungha revision `ca-elb-dashboard--0000032` in sub
  `577d6332-…` is still active + healthy. Decision deferred to the operator —
  whether to leave as-is or roll back / tear down `rg-elb-dashboard` in jungha
  depends on whether anyone else relies on that environment.
- The `az-jungha` shell alias and `~/.azure-jungha` profile are intentionally
  untouched. The operator can `rm -rf ~/.azure-jungha` and remove the alias
  manually when they no longer need that profile.
- `.github/copilot-instructions.md` / `AGENTS.md` carry no jungha-specific
  guidance to delete; the agent's stale "use az-jungha" user-memory note has
  already been corrected in `/memories/azure-context.md`.
