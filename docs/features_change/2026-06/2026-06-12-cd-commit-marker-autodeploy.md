# `[cd]` commit marker triggers approval-free auto-deploy

## Motivation

The maintainer wants a push to deploy itself when the commit message opts in,
without clicking the production approval prompt every time, while keeping
ordinary pushes build-only and keeping hand-run deploys reviewable.

## User-facing change

- A push to `main` whose commit message contains the literal marker `[cd]`
  now auto-deploys all sidecars (at the immutable `gha-<sha>` tag the build
  produced) **without the production approval gate**, as soon as Build Images
  succeeds.
- A push **without** `[cd]` builds images but does NOT deploy (the deploy job
  is skipped) — previously every successful build offered an approval-gated
  deploy.
- Manual `workflow_dispatch` deploys are unchanged: they still go through the
  `production` environment approval gate.

## API / IaC diff summary

- `.github/workflows/deploy.yml` only:
  - Job `if` now requires `workflow_dispatch` OR (`workflow_run` success AND
    `contains(head_commit.message, '[cd]')`).
  - `environment.name` is `production` for manual dispatch and an empty string
    (= no environment, no approval) for the automatic `[cd]` path.
  - Header/inline comments document the approval matrix and the safety rails.

## Approval matrix

| Trigger | Environment | Approval |
|---|---|---|
| `workflow_dispatch` (manual) | `production` | required |
| `workflow_run` + `[cd]` in commit | (none) | none — auto-deploy |
| `workflow_run`, no `[cd]` | — | job skipped (build only) |

## Safety rails on the no-approval path

`main` branch only (existing `workflow_run.branches`), build-success only, the
chosen image tag must exist in ACR, and the post-deploy `/api/health` smoke must
pass or the run goes red. There is no automatic rollback — a red CD run is the
signal to roll back via the manual workflow.

## Validation evidence

- `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy.yml'))"`
  parses; the `if` and conditional `environment.name` are well-formed.
- Behavioural verification happens on the next app-code push with `[cd]` (the
  `build-images.yml` paths filter means a workflow-only change does not trigger
  the build/deploy chain).
