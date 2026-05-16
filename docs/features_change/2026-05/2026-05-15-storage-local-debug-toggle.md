# Local-debug toggle for Storage publicNetworkAccess

**Date**: 2026-05-15
**Scope**: developer experience + policy

## Motivation

The §9 production posture (`publicNetworkAccess: Disabled` on every workload
Storage account, reachable only via private endpoint) is the right call for a
deployed environment, but it makes BLAST Databases / Queries / Results
**impossible to exercise from a developer laptop** — every blob list/read
returns `AuthorizationFailure`, and the dashboard renders the
`network_blocked` degraded state we added in
[2026-05-15-storage-failure-classifier.md](./2026-05-15-storage-failure-classifier.md).

User feedback (2026-05-15):

> 로컬에서 디버깅 할때는 필요하다면 storage account 의 public network 을
> 오픈하고 모든 기능이 정상 동작해야해 실제로..

i.e. local debugging must be able to actually exercise every code path,
even the ones that hit the Storage data plane. Without an explicit
sanctioned escape hatch, future contributors (or future me) will either:

1. flip `publicNetworkAccess: Enabled` ad-hoc via the portal and forget to
   close it (production drift), or
2. add an environment toggle / dashboard button (which the §9 policy
   explicitly forbade, and which encourages "leave it on" antipatterns).

## User-facing change

A new manual shell script:

```
scripts/dev/storage-public-access.sh on   [--account NAME] [--rg NAME] [--ip IP] [--subscription ID]
scripts/dev/storage-public-access.sh off  [--account NAME] [--rg NAME]                [--subscription ID]
scripts/dev/storage-public-access.sh status [...]
```

`on` flips the workload Storage account to:

- `publicNetworkAccess = Enabled`
- `networkAcls.defaultAction = Deny`
- `networkAcls.ipRules = [<caller IP>]`  (auto-detected via `api.ipify.org`,
  or supplied via `--ip`)
- `bypass = AzureServices`

i.e. the data plane becomes reachable **only from the developer's current
public IP**. Entra ID auth + RBAC are unchanged — the `az login` identity
must already hold `Storage Blob Data Reader` (or higher).

`off` reverts to the production posture (`publicNetworkAccess = Disabled`,
ipRules cleared).

`status` prints the current network state without changing anything.

The dashboard's degraded `network_blocked` message now points at the
script with a copy-pasteable command.

## Policy delta

- §9 of [.github/copilot-instructions.md](../../../.github/copilot-instructions.md)
  reworded: production stays `Disabled`; the local-debug exception is
  explicitly the manual script (not a dashboard button, not an env var).
  Acceptable transient state: `Enabled` + `defaultAction=Deny` + non-empty
  `ipRules`. Incident states: `Enabled` + `defaultAction=Allow`, or any
  `Enabled` left over in a deployed environment.
- Tripwire #8 in [AGENTS.md](../../../AGENTS.md) reworded the same way.
  Future agents are now told the script is the **only** sanctioned path —
  do not bypass with wider IP ranges, `--default-action Allow`, or
  `bypass: AzureServices` outside what the script does.

## Why a script and not a UI button

A dashboard button is friction-free and tempting to leave on. A shell
command requires conscious intent (and shows up in shell history /
auditable terminal recordings). The friction **is** the safety mechanism.
If the script proves too painful in practice, we can revisit, but
defaulting to the lower-friction option first would be a one-way door.

## API / IaC diff summary

| File | Change |
|------|--------|
| [scripts/dev/storage-public-access.sh](../../../scripts/dev/storage-public-access.sh) | NEW — on/off/status commands wrapping `az storage account update` + `az storage account network-rule add/remove`. Auto-detects caller IP. Resets stale ipRules on each `on` so the allowlist never accumulates. |
| [.github/copilot-instructions.md](../../../.github/copilot-instructions.md) §9 | Reworded — production posture unchanged, local-debug exception explicitly documented. |
| [AGENTS.md](../../../AGENTS.md) tripwire #8 | Reworded to point at the script as the only sanctioned path. |
| [api/services/storage_data.py](../../../api/services/storage_data.py) `classify_storage_failure` | The `network_blocked` degraded message now tells the operator how to open the surface locally instead of just saying "run azd up". |

No Bicep change. The Bicep modules continue to deploy with
`publicNetworkAccess: Disabled` — the script mutates the live account
out-of-band (and `azd provision` would revert the mutation, which is the
intended behaviour: closing the surface is the safe default).

No new dependency. Script uses `az`, `jq`, `curl` — all already in the
preflight check.

## Validation

- `bash -n scripts/dev/storage-public-access.sh` — syntax clean.
- `scripts/dev/storage-public-access.sh status` — verified against the
  live `elbstg01` account; correctly reads
  `{"bypass":"AzureServices","defaultAction":"Allow","ipRules":[],"public":"Disabled"}`.
- `uv run pytest -q api/tests` — 67 passed.
- Live `on`/`off` cycle was **not** auto-run; per the new §9 policy, the
  toggle should only be flipped when an actual debugging task needs it.
  The operator running `on` should immediately follow with their debug
  work and `off`.

## Follow-ups (out of scope here)

- The Storage card in the SPA still renders `publicNetworkAccess=Enabled`
  as a hard incident with no nuance. Once a real local-debug session
  proves the script's UX, we should teach the card to recognise the
  acceptable transient state (`Enabled` + `defaultAction=Deny` +
  non-empty `ipRules`) and surface a banner like "local-debug window open
  for IP X — run `storage-public-access.sh off` when done" instead of a
  red incident pill.
- ACR build pipeline is still inert in local dev (worker not
  auto-started). That's tracked separately — the local 6-sidecar Compose
  in [scripts/dev/docker-compose.full.yml](../../../scripts/dev/docker-compose.full.yml)
  is the intended fix.
