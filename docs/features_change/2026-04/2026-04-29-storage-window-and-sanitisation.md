# 2026-04-29 — Storage public-access window + sanitisation

## Motivation
Two §12 security checklist items were not yet enforced:

1. Storage account `publicNetworkAccess` could be left `Enabled` if a
   user clicked the toggle and forgot to flip it back. `azure-prereq.md`
   §9 requires it to default to `Disabled`.
2. Run-Command output (and any future `az`/`kubectl` echo) was returned
   to the SPA verbatim, which could leak SAS query strings, bearer
   tokens, or full subscription ids if upstream tooling logged them.

## User-facing change
- New endpoint `POST /api/monitor/storage/public-access/window` starts a
  Durable orchestrator that enables public access, waits a TTL
  (default 5 min, propagation 15 s), and re-disables on completion or
  failure. Response is the standard DF check-status envelope.
- `check_cloud_init_activity` now sanitises Run Command output before
  returning it to the orchestrator (and ultimately the SPA).

## API/IaC diff summary
- `api/orchestrators/storage_window.py` (new) — orchestrator with
  guaranteed re-disable.
- `api/activities/storage.py` (new) — single activity wrapping the
  storage SDK.
- `api/services/sanitise.py` (new) — masks SAS query strings, bearer
  tokens, account/access keys, client secrets, and Azure GUIDs.
- `api/function_app.py` — registers the new orchestrator + activity, and
  a new `start_storage_public_access_window` HTTP starter.
- `api/activities/terminal.py` — applies `sanitise()` to Run Command
  output and trims to 1000 chars.
- `api/tests/test_sanitise.py` (new) — 5 cases covering each pattern.

## Validation evidence
- `pytest -q` → 13 passed (was 8).
- `ruff check api` → All checks passed.
- Function inventory: 19 functions registered (was 16).

## Follow-ups
- Wire the SPA Storage card "Enable for 5 min" button to the new
  windowed endpoint instead of the raw toggle.
- Add an audit log table (Durable Entity) for every public-access flip.
