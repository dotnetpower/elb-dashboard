---
title: Message Flow — keep running jobs visible, fade finished ones, animate live energy
description: Broaden the message-flow active set, add a settling window for recently-terminal jobs, and add status-aware visuals plus moving particles to the constellation.
tags:
  - ui
  - blast
---

# Message Flow — settling window + status visuals + energy particles

## Motivation

A BLAST job appeared on the dashboard **Message Flow** card when it started and
then **vanished mid-run**, and a finished/failed job was dropped the instant it
left the active set — there was no "running" persistence, no error state, and no
sense of live activity. Root cause: the card's active set was only
`{queued, running}`, so a job entering the canonical `reducing` (result-merge)
phase — or the freshly-submitted `pending` state — silently disappeared, and
terminal jobs were removed immediately.

## User-facing change

- **Running jobs stay visible for their whole lifecycle.** The active set now
  matches the canonical in-flight set `{queued, pending, running, reducing}`
  used everywhere else (`JobStateRepository.list_active`, auto-stop). A
  `reducing` job no longer vanishes.
- **Finished/failed jobs linger and fade out** instead of being yanked. A
  recently-terminal job (`completed`/`failed`/`cancelled`) stays as a
  `settling` box for a short window (default 90s, `MESSAGE_FLOW_SETTLING_SECONDS`)
  so the operator can see it finished or failed before it fades. These are real
  jobstate rows, never fabricated, and do not inflate the active counts.
- **Status-aware visuals.** Failed jobs read in a danger tone with a broken-cross
  marker (colour is never the only signal); reducing pulses slower as "merging";
  queued/pending stay dashed; completed/cancelled use a calm done/neutral tone.
- **Moving energy particles.** Glowing dots travel producer → job → cluster along
  active links (running/reducing carry two faster particles, queued one slow
  drift), so a live run reads as energy in motion. Running links stay bright
  regardless of age (the old age-only fade made a long run look vanished).
  Everything honours `prefers-reduced-motion` (no particles, static halos).
- **Card stays populated while anything is active OR settling**, collapsing to
  the calm "no active messages" line only when truly idle. It also polls faster
  (8s) while work is in flight and backs off (20s) when idle.
- New legend entries (reducing, failed, finishing/fading, moving dots = live
  energy); the job detail surfaces an error code when present.

## API / IaC diff summary

- `GET /api/monitor/message-flow` snapshot additions (all optional, backward
  compatible): `settling_total`; per-broker-box `updated_at`, `lifecycle`
  (`active`/`settling`), `error_code`; per-cluster `settling`.
- New env var `MESSAGE_FLOW_SETTLING_SECONDS` (default 90, bounded 1–3600).
- No IaC change.

## Validation evidence

- Backend: `uv run pytest -q api/tests/test_message_flow.py` → 13 passed
  (added: pending/reducing active, settling-without-inflating-counts, old
  terminal excluded, env-override). `test_route_contracts.py` green.
- Frontend: `npx vitest run src/components/cards/MessageFlow` → 30 passed
  (added colors `statusTone`/`isErrorStatus`/`jobTone` + tooltip settling/error
  cases). `npm run build` clean; `eslint` clean.
- `uv run ruff check` clean on the changed Python files.
</content>
</invoke>
