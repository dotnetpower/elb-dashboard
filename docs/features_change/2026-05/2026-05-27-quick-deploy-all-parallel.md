# `quick-deploy.sh all` — parallel image builds

## Motivation

`scripts/dev/quick-deploy.sh all` previously recursed into itself once per
target (`api`, then `frontend`, then `terminal`), running three sequential
`az acr build` jobs. With per-image build times of 30–90 s and a 75 s
ACR-firewall settle on the first call, the full "deploy every custom
image" cycle took roughly 6–8 minutes — about double what it needed to.

## User-facing change

`quick-deploy.sh all` now opens the ACR firewall **once** in the parent
process, spawns the three `az acr build` jobs **in parallel** with per-image
log files at `.logs/quick-deploy/<tag>/build-<image>.log`, polls every
15 s with a status line per still-running job, and dumps the last 30 lines
of any failing log inline. After all three builds finish, it restores the
ACR firewall and then PATCHes the five Container App containers
(`api` / `worker` / `beat` / `frontend` / `terminal`) **sequentially** to
avoid the read-modify-write race on `az containerapp update --container-name`.

Net wall-time on the deployed `ca-elb-dashboard` shape: ~3–4 minutes
(bounded by the slowest single image build) vs ~6–8 minutes previously.

No flag change. Existing callers — including the `--logs` and
`--rebuild-terminal-base` flags and the `<tag>` positional argument used by
the rollback hint — continue to work as before.

## API / IaC diff summary

- `scripts/dev/quick-deploy.sh` (`SIDECAR == "all"` branch): replaced the
  recursive-sequential loop with an inline parallel-build implementation
  modelled on `scripts/dev/postprovision.sh` (same `&` + `wait` + polling
  + per-log-file pattern). ACR firewall is now opened once via
  `acr_ensure_build_access` and closed via a single `trap` + explicit
  `acr_restore_build_access` after the build phase succeeds.
- `terminal_base_image` resolution (`ensure_terminal_base_image` +
  `terminal_base_image`) is invoked in the parent before spawning the
  three build subshells, so two terminal builds cannot race on the base
  image's `az acr import` / `az acr build`.
- `docs/operate/cli-upgrade.md`: "When to use which path" row and the
  "Refresh all custom images" workflow section now describe parallel
  builds + sequential PATCHes and the rationale for the latter.
- No Bicep / API / Python / frontend changes.

## Validation evidence

- `bash -n scripts/dev/quick-deploy.sh` → syntax OK.
- `uv run mkdocs build --clean` → no new warnings on the edited page.
- Consumer search (`grep -RInE 'quick-deploy\.sh\s+all'`) — three call
  sites: the script's own help block, the rollback hint it prints, and
  the `cli-upgrade.md` workflow examples. All continue to use the
  unchanged CLI surface (`<scope> [tag] [--logs] [--rebuild-terminal-base]`).

## Risks / non-goals

- PATCHes intentionally stay sequential. `az containerapp update
  --container-name X` is read-modify-write against the same Container App
  template with no ETag protection; running three in parallel would
  silently revert some sidecars on the final active revision.
- No change to `cli-upgrade.sh full` (Bicep template swap is still the
  right tool when sidecar layout, secrets, probes, scale rules, or the
  terminal base image changed).
- No change to the snapshot / `/api/health` auto-rollback envelope. This
  script remains the fast unsafe path; for guarded production deploys
  keep using `cli-upgrade.sh full`.
