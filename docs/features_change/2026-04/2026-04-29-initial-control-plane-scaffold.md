# 2026-04-29 — Initial scaffold of the browser-only control plane

## Motivation
Bring up an end-to-end skeleton that lets a researcher provision a Remote
Terminal VM from the browser, sign in with `az login` once on the VM, and
run `elastic-blast` against ElasticBLAST on Azure infrastructure that the
web UI also monitors.

## User-facing change
- New SPA routes: `/` (Dashboard), `/terminal` (Remote Terminal).
- Microsoft Entra sign-in via MSAL Auth Code + PKCE; SPA acquires an access
  token for our Function App; backend uses OBO to call Azure as the user.
- Dashboard cards: AKS, Storage (with public-access toggle), ACR, Remote
  Terminal — all polled every 30 s via TanStack Query.
- Remote Terminal page: form to start provisioning, live phase tracker,
  one-shot password reveal + copy, ssh command copy.
- Cloud-init prepares the VM (`az`, `kubectl`, `azcopy`, Python 3.11 venv,
  cloned `elastic-blast-azure`) so the user only has to run
  `az login --use-device-code` after SSHing in.

## API/IaC diff summary
### IaC (`infra/`)
- `main.bicep` (subscription scope) creates the platform RG.
- `modules/platform.bicep`:
  - Function App (Linux Y1 Consumption, Python 3.11) with system-assigned MI.
  - Storage Account for Functions runtime + Durable Functions hub.
  - Key Vault (RBAC mode, soft-delete + purge protection) for VM secrets.
  - Static Web App (Standard) linked to the Function App backend.
  - App Insights + Log Analytics.
  - RBAC role assignments: Function MI → KV Secrets Officer, Storage Blob
    Owner, Queue/Table Data Contributor; user (azd principal) → KV.

### API (`api/`)
- Python v2 `function_app.py` registers:
  - `GET /health`, `GET /me` (whoami after JWT validation).
  - `POST /terminal/provision` → starts Durable orchestrator
    `provision_terminal_orchestrator_trigger`.
  - `GET /terminal/status/{instance_id}` → orchestration status.
  - `GET /terminal/{vm_name}/password` → one-shot KV secret reveal.
  - `GET /monitor/aks|storage|acr|terminal` → read-only monitoring.
  - `POST /monitor/storage/public-access` → toggle.
- Durable orchestrator activities (all idempotent):
  `ensure_resource_group_activity`, `ensure_network_activity` (VNet/NSG
  with caller-IP-only SSH, Public IP with DNS label, NIC),
  `generate_password_activity` (24-char password to KV),
  `create_vm_activity` (Ubuntu 24.04, custom_data = cloud-init),
  `check_cloud_init_activity` (Run Command poll).
- `auth/token.py` validates bearer tokens against tenant OIDC discovery /
  JWKS with RS256; `auth/obo.py` issues OBO downstream tokens.
- `services/` wraps Azure SDK (network, compute, keyvault, storage, ACR,
  AKS); orchestrators/activities never import `azure.mgmt.*` directly.
- `services/image_tags.py` — single source of truth for ElasticBLAST image
  tags (`ncbi/elb:1.4.0`, `ncbi/elasticblast-job-submit:4.1.0`,
  `ncbi/elasticblast-query-split:0.1.4`).

### Web (`web/`)
- Vite + React 18 + TS strict, MSAL React, TanStack Query, lucide-react.
- Glassmorphic theme tokens in `src/theme/glass.css` (deep-navy gradient,
  blurred translucent cards, low-saturation accents).
- Typed API client in `src/api/`; cards in `src/components/cards/`.
- `staticwebapp.config.json` for SPA navigation fallback + security headers.

### Cloud-init (`scripts/cloud-init/remote-terminal.yaml`)
Idempotent install of `azure-cli`, `kubectl` (snap), `azcopy`, Python 3.11,
clones `elastic-blast-azure`, creates venv + `pip install`, writes
`/etc/profile.d/elb-env.sh` with `PYTHONPATH`, `AZCOPY_AUTO_LOGIN_TYPE`,
`ELB_SKIP_DB_VERIFY`, `ELB_DISABLE_AUTO_SHUTDOWN`. MOTD instructs the user
to run `az login --use-device-code` next. Marks
`/var/lib/cloud/elb-bootstrap.done` for the orchestrator's poll.

## Validation evidence
- `pytest -q` → 8 passed (`api/tests/test_passwords.py`,
  `api/tests/test_models.py`).
- `az bicep build --file infra/main.bicep` → compiles with no errors
  (warnings about a newer Bicep CLI release only).
- `get_errors` over all `api/` and `web/src/` files → no diagnostics.

## Follow-ups
- xterm.js + WebSocket SSH proxy for the embedded Remote Terminal shell.
- AKS / ACR / Storage orchestrators for `ensure_resource_groups`,
  `ensure_acr`, `build_acr_images`, `ensure_storage` (currently only
  read-only monitoring is wired).
- ElasticBLAST Job orchestrator (`monitor_jobs`).
- App Registration provisioning script + docs/auth.md.
