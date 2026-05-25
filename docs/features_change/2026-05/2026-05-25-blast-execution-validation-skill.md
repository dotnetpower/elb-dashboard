# BLAST Execution Validation Skill

## Motivation

BLAST execution validation spans the dashboard submit path, the sibling OpenAPI submit path, Celery queue behavior, Playwright E2E coverage, Azure login state, and Application Insights telemetry. Agents needed a repeatable workflow that starts with safe local checks, escalates to live submits only with explicit scope, and loops through fix/rerun cycles autonomously.

## User-Facing Change

Added a project-scoped Copilot skill named `blast-execution-validation`. It can be invoked for BLAST execution test planning, parallel queue validation, OpenAPI submit hardening, App Insights error hunts, and autonomous E2E run/fix/rerun work.

The skill now defines concrete handling for `scope`, `concurrency`, and `max-hours`. Explicit `scope: full-azure` counts as approval for the guarded lifecycle validation run, while `concurrency` is reserved for post-lifecycle submit fan-in probes and `max-hours` is treated as a hard autonomous run budget.

## API/IaC Diff Summary

- No runtime API or IaC changes.
- Added `.github/skills/blast-execution-validation/SKILL.md`.
- Added skill references for the scenario matrix, App Insights KQL, and autonomous loop rules.
- Clarified full Azure invocation behavior for `/blast-execution-validation scope: full-azure concurrency=2 max-hours=4`.

## Validation Evidence

- Frontmatter and file placement checked with shell validation.
- The skill references existing validation entry points: `api/tests/test_external_blast_api.py`, `api/tests/test_blast_submit_route_options.py`, `api/tests/test_blast_queue.py`, `api/tests/test_blast_tasks.py`, `api/tests/test_openapi_rate_limit.py`, `scripts/dev/e2e-ui.sh`, and the Playwright E2E scripts under `scripts/e2e/scenarios/`.