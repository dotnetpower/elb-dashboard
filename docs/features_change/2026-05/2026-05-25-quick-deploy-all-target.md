# Quick Deploy All Target

## Motivation

Operators sometimes need to fast-deploy every code image for the bundled [Azure Container Apps](https://learn.microsoft.com/azure/container-apps/overview) control plane after touching frontend, backend, and terminal code together. Before this change, `quick-deploy.sh` only accepted one target at a time, so a full code-image refresh required remembering the correct sequence manually.

## User-facing change

`scripts/dev/quick-deploy.sh all` now runs the complete fast-deploy sequence in one command:

```bash
scripts/dev/quick-deploy.sh all
```

The `all` target deploys `api`, `frontend`, and `terminal` in sequence. The existing `api` target still patches the `api`, `worker`, and `beat` containers together because they share the `elb-api` image. A custom tag is reused across all three image repositories, and `--rebuild-terminal-base` is forwarded only to the terminal deploy.

## API / IaC diff summary

- No API or IaC changes.
- Updated `scripts/dev/quick-deploy.sh` usage, examples, target validation, and dispatch logic to accept `all`.
- `--logs` with `all` waits until all deploys finish, then tails the `api` container logs instead of blocking after the first child deploy.

## Validation evidence

- `bash -n scripts/dev/quick-deploy.sh` passed.
- No live deploy was run for this script-only change.