---
title: Auto-offer deploy after a successful image build (still approval-gated)
description: Build Images success now auto-triggers the Deploy workflow, which waits for the production approval e-mail before patching the Container App. Manual deploy is unchanged.
tags:
  - operate
  - infra
---

# Auto-offer deploy after a successful image build

## Motivation

Builds already ran automatically on every push to main
([build-images.yml](../../../.github/workflows/build-images.yml)), but deploying
the freshly built images required remembering to open the **Deploy to Container
App** workflow by hand and typing the tag. The maintainer wanted a hands-off
prompt: when a build finishes, get an approval e-mail, and a single click ships
it — while keeping the existing manual deploy path for ad-hoc / rollback cases.

## User-facing change

- After **Build Images** succeeds on `main`, the **Deploy to Container App**
  workflow now starts automatically (via `workflow_run`) for **all** sidecars at
  the immutable `gha-<sha>` tag the build just produced.
- It does **not** deploy on its own: the job targets the `production`
  environment, whose required-reviewer rule makes GitHub e-mail the reviewer
  (`dotnetpower`) a **Review deployments** request. Nothing reaches Azure until
  you click **Approve** (in the run or the e-mail). **Reject** discards an
  auto-deploy you don't want.
- The existing **manual** path is unchanged — `workflow_dispatch` from the
  Actions UI still lets you pick a specific sidecar and tag, and it hits the same
  approval gate.
- A failed or cancelled build never offers a deploy (job-level
  `if: …workflow_run.conclusion == 'success'`).

## API / IaC diff summary

Only [.github/workflows/deploy.yml](../../../.github/workflows/deploy.yml)
changed:

- **New trigger** `on.workflow_run` → `workflows: ["Build Images"]`,
  `types: [completed]`, `branches: [main]`, in addition to the existing
  `workflow_dispatch`.
- **New job guard** `if: github.event_name == 'workflow_dispatch' ||
  github.event.workflow_run.conclusion == 'success'`.
- **New "Resolve deploy parameters" step** sets `DEPLOY_SIDECAR` /
  `DEPLOY_TAG` / `DEPLOY_SKIP_HEALTH`: manual dispatch uses the typed inputs;
  the auto path defaults to `all` sidecars at `gha-<head_sha[:7]>` with the
  health smoke enabled. All downstream steps (ACR tag verify, patch, smoke,
  summary) now read those env vars instead of `inputs.*`.
- **Checkout** pins `ref: github.event.workflow_run.head_sha || github.ref` so
  the auto path runs the deploy scripts from the build's commit.
- No Bicep / Azure resource change. The `production` environment + required
  reviewer (the e-mail mechanism) already existed.

## Operational note

Every push to `main` now produces one approval request after its build. If that
becomes noisy, scope the auto path later (e.g. gate on release tags) — the
manual path is always available regardless. Make sure the reviewer's GitHub
notification settings have **Actions → Required deployment reviews** e-mail
enabled, otherwise the prompt only shows in the Actions UI.

## Validation evidence

- `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy.yml'))"`
  parses clean; `on` keys = `['workflow_dispatch', 'workflow_run']`,
  `workflow_run.workflows == ['Build Images']` (matches the build workflow's
  `name:`).
- `gh api repos/dotnetpower/elb-dashboard/environments/production` confirms the
  `required_reviewers` rule with reviewer `dotnetpower` is active, so both
  trigger paths e-mail for approval before `az containerapp update`.
