# 2026-05-14 — Terminal sidecar replaces the Remote Terminal VM

## Motivation

The user asked whether the Remote Terminal VM could be replaced by a small
sidecar in the bundled Container App. After inventorying every responsibility
of the current `vm-elb-terminal` (cloud-init, tools, auth, SSH, NSG,
password, lifecycle endpoints), the answer is yes — and most of the
operational complexity simply disappears with the VM.

## User-facing change

None at runtime (planning document update). For users this becomes
significantly simpler:

- Old: open dashboard → wait 10–15 min for VM provisioning → reveal admin
  password → open NSG to your IP → SSH from a separate terminal app or the
  embedded xterm shell.
- New: open dashboard → click Terminal tab → MSAL + tenant-role check →
  bash prompt with `elastic-blast` ready to run.

## Inventory of Remote Terminal VM responsibilities (from current code)

Sources: [scripts/cloud-init/remote-terminal.yaml](scripts/cloud-init/remote-terminal.yaml),
[api/orchestrators/provision_terminal.py](api/orchestrators/provision_terminal.py),
[api/routes/terminal.py](api/routes/terminal.py).

| Category | Responsibility | Replacement in `terminal` sidecar |
|----------|----------------|-----------------------------------|
| Tools | `azure-cli`, `kubectl`, `azcopy`, `git`, `make`, `jq`, `python3.11`, `python3.11-venv`, `python3-pip`, `primer3`, `tmux`, `unzip`, `curl`, `gnupg`, `lsb-release`, `apt-transport-https`, `ca-certificates`, `software-properties-common` | All baked into the `elb-terminal:<tag>` image at build time, with retry/failure handling moved to CI (no more 10-min Defender-onboarding race in cloud-init). |
| App | Clone `dotnetpower/elastic-blast-azure` to `~/elastic-blast-azure` | Baked into the image at `/opt/elb/elastic-blast-azure` (immutable). A read-only convenience copy is also surfaced under `/home/azureuser/elastic-blast-azure` via the Azure Files mount. |
| App | `python3.11 -m venv` + `pip install -r requirements/test.txt` + `azure-mgmt-*` SDKs + `pip install --no-build-isolation --no-deps elastic_blast` | All baked into the image; venv at `/opt/elb/venv`. |
| Env | `PYTHONPATH=src:$PYTHONPATH`, `AZCOPY_AUTO_LOGIN_TYPE=MSI`, `ELB_SKIP_DB_VERIFY=true`, `ELB_DISABLE_AUTO_SHUTDOWN=1` | Same content in `/etc/profile.d/elb-env.sh` baked into the image. |
| Auth | `elb-az-login-mi` script doing `az login --identity` from IMDS at shell login | Same script; uses Container Apps' MI endpoint (`IDENTITY_ENDPOINT`/`IDENTITY_HEADER`) instead of IMDS. End result (`az account show` works) is identical. |
| Auth | MOTD pointing at `az login --use-device-code` for personal identity | Same MOTD baked into the image; explains that MI is the default and personal identity override is per-session. |
| Persistence | `~/elastic-blast-azure`, `~/.azure/`, `~/.kube/`, user files persisted on the VM OS disk | Azure Files share `terminal-home` mounted at `/home/azureuser`. Survives revision restart and image rebuild. |
| SSH | `Port 22 / Port 443` with `PasswordAuthentication yes` | **Removed.** No SSH. Browser → api WebSocket → ttyd loopback. |
| NSG | `AllowSSH` rule scoped to caller IP via `/api/terminal/{vm}/open-ssh` | **Removed.** No NSG. |
| Secrets | Per-VM admin password generated, stored in Key Vault, revealed once via `/api/terminal/{vm}/password` | **Removed.** No password. Access gated by MSAL + tenant role on WebSocket upgrade. |
| Provisioning | `provision_terminal_orchestrator` Durable orchestrator: ensure_resource_group → ensure_network → ensure_keyvault → generate_password → create_vm → assign_vm_roles → wait for cloud-init | **Removed.** Provisioning is `azd up` + revision rollout. Per-user state isolation moves to per-tmux-window inside the shared sidecar. |
| Lifecycle | `/api/terminal/{vm}/start` (deallocate) | **Removed.** Sidecar lifecycle == Container App revision lifecycle. |
| Lifecycle | `/api/terminal/{vm}/stop` (deallocate) | **Removed** for the same reason. |
| Lifecycle | `/api/terminal/{vm}/destroy` (delete VM, NIC, IP, KV secret) | **Removed.** No per-user resource to delete; image redeploy is the equivalent. |
| Health | `/api/terminal/{vm}/health` (power state, cloud-init progress) | Replaced by Container App revision health + cheap `GET /api/terminal/health` that pings `127.0.0.1:7681`. |
| Browser shell | xterm.js → SSH (or Bastion later) | xterm.js → `WS /api/terminal/ws` → MSAL + role check → duplex-copy to loopback ttyd. tmux gives session continuity across browser refreshes. |

## Architecture diff summary

| Area | Previous (4 sidecars + VM) | Now (5 sidecars, no VM) |
|------|----------------------------|-------------------------|
| Compute | 4 sidecars + 1 Linux VM (Standard_D4s_v5 or similar) | 5 sidecars in one revision |
| Sidecar set | api, worker, beat, redis | api, worker, beat, redis, **terminal** |
| Container App resource budget (initial) | 0.5 vCPU / 1 GiB | 1.0 vCPU / 2 GiB (terminal carries the toolchain) |
| Subnets | containerapps, private-endpoints, aks, terminal, bastion | containerapps, private-endpoints, aks (terminal + bastion subnets removed) |
| Identities | `id-elb-control`, `id-elb-terminal`, `id-elb-openapi` | `id-elb-control` (now also covers AcrPull + AKS Cluster User), `id-elb-openapi` (terminal MI removed) |
| Secrets | VM admin password in Key Vault | None for the terminal |
| New Azure Files shares | `redis-data` | `redis-data`, **`terminal-home`** |
| Cloud-init | 10-15 min bootstrap with Defender retry logic | Replaced by image build at CI time |
| SSH | Public IP, port 22 + 443, password auth, NSG allow-list | None |
| Browser path to shell | xterm.js → SSH (TCP/22 over public IP, NSG-filtered) | xterm.js → `wss://<api>/api/terminal/ws` → MSAL + tenant role → loopback ttyd |
| Per-user provisioning | Required (`/api/terminal/provision` Durable orchestrator) | None |
| Endpoints removed | n/a | `terminal/provision`, `terminal/status/{instance_id}`, `terminal/{vm}/start`, `/stop`, `/destroy`, `/password`, `/open-ssh`, `/health` (replaced by `/api/terminal/health` and `WS /api/terminal/ws`) |

## Why a single shared tmux is acceptable for now

The expected user count is 1–2 operators. A single shared tmux session
attached via `ttyd ... -W tmux new -A -s elb` gives session continuity for
free and keeps the proxy contract trivial. If a second operator shows up
regularly:

- Switch to `tmux new -A -s elb-${owner_oid}` (the api sidecar passes the
  validated `owner_oid` to the terminal as an env var on each WebSocket
  upgrade).
- This is a one-line change; documented in the Risks table.

## Files changed

- `docs/container-apps-migration.md`:
  - Decision Summary updated to describe **five** sidecars with a dedicated
    "No separate Remote Terminal VM" bullet.
  - Cost-minimisation choice updated (1.0 vCPU / 2 GiB total).
  - "Explicitly removed from the prior plan" gains four rows: Remote Terminal
    VM, terminal/bastion subnets, per-VM admin password + reveal flow, and
    a clearer note that the bundled topology now also collapses the third
    Container App.
  - Resources to Create updated: removed Remote Terminal VM, removed
    `id-elb-terminal`, added `terminal-home` Azure Files share, removed
    `snet-terminal` and `snet-bastion`.
  - Storage Network Isolation rules: workload-storage access from the
    terminal sidecar is via the same `snet-containerapps` subnet path as the
    rest of the bundle.
  - Networking subnets table reduced to three subnets (containerapps,
    private-endpoints, aks).
  - Target Architecture diagram updated to show the fifth sidecar and the
    second Azure Files mount.
  - Component Plan table replaces the "Remote Terminal | VM plus optional
    gateway" row with a detailed `terminal` sidecar row.
  - **Service Boundaries: the previous `terminal-gateway` section is
    replaced by a comprehensive `terminal` sidecar section** that documents
    the image build, ttyd config, auth on the WebSocket, Azure auth from
    inside the shell, persistence, lifecycle, and a full inventory mapping
    every Remote Terminal VM responsibility to the sidecar replacement,
    with verification tests.
  - Storage Plan: the platform Storage account now also hosts the
    `terminal-home` file share.
  - Identity Plan: collapsed to one MI for the Container App
    (`id-elb-control`), updated to include AcrPull + AKS Cluster User; the
    `id-elb-terminal` row is removed.
  - Route Migration Map updates every `terminal/*` row, marks the
    deprecated endpoints, and adds the new `WS /api/terminal/ws`.
  - Phase 2 picks up the terminal sidecar work and the Azure Files share.
  - Phase 3 calls out Remote Terminal VM deletion explicitly.
  - Cutover Checklist gains four new rows: terminal-home mount works while
    storage is private, browser opens working bash with all tools
    responding, tmux reattaches across browser refresh, no SSH endpoint
    exists, no Microsoft.Compute VM remains in the terminal RG.
  - Risks table gains four new rows for terminal-specific risks (browser
    session loss on revision restart, multiple operators, image weight,
    WebSocket auth bypass).
  - Open Decisions: "Terminal access" row replaced by a positive statement
    of the chosen design.
  - First Implementation Slice notes the terminal sidecar arrives in phase
    2.
- `README.md`: Architecture Planning bullet updated to mention the terminal
  sidecar and call out "no Remote Terminal VM, no SSH, no admin password".

## Code consequences (follow-up tickets)

These are not done in this PR (planning only) but are the obvious follow-ups:

1. Build the `elb-terminal` image (Dockerfile based on Ubuntu 22.04;
   replicate the cloud-init apt + pip + clone + venv steps as deterministic
   image build steps).
2. Add the `terminal` sidecar to the Container App definition with the
   `terminal-home` Azure Files volume mount and `127.0.0.1:7681` ttyd
   process.
3. Add `WS /api/terminal/ws` in the api sidecar (FastAPI WebSocket route +
   MSAL validate + tenant-role check + duplex copy to loopback).
4. Add `GET /api/terminal/health` (loopback TCP probe + revision-state
   passthrough).
5. Delete the per-VM terminal API surface
   ([api/routes/terminal.py](api/routes/terminal.py)),
   [api/orchestrators/provision_terminal.py](api/orchestrators/provision_terminal.py),
   [api/activities/terminal.py](api/activities/terminal.py),
   [services/network.py](api/services/network.py) NSG SSH rule helpers,
   [services/passwords.py](api/services/passwords.py),
   [services/keyvault.py](api/services/keyvault.py) password reveal helpers,
   and the SSH glibc / `services/ssh_exec.py` helper.
6. Delete [scripts/cloud-init/remote-terminal.yaml](scripts/cloud-init/remote-terminal.yaml)
   once the new image is in production.
7. Update the SPA Terminal tab to open the new WebSocket instead of the
   xterm.js + SSH proxy flow; remove the password-reveal UI and the
   open-NSG-to-my-IP UI.
8. Update [api/services/image_tags.py](api/services/image_tags.py) to
   include the new `elb-terminal` tag.
9. Add the verification tests listed in the doc to the api `pytest` suite.

## Validation evidence

Documentation-only change. Verified the doc no longer recommends provisioning
a Remote Terminal VM:

```bash
grep -nE "vm-elb-terminal|Remote Terminal VM|snet-terminal|id-elb-terminal|terminal-gateway" \
  docs/container-apps-migration.md
```

Remaining matches are inside the "Explicitly removed from the prior plan"
table, the "What this sidecar replaces" inventory, and the Resources-to-Create
"Not created" line — all pointing at deletion. No active recommendation
provisions a terminal VM.
