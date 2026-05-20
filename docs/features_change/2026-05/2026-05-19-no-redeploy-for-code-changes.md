# 2026-05-19 — Charter guardrail: do not redeploy for ordinary code changes

## Motivation

Past sessions have shown agents (and humans under time pressure) running
`scripts/dev/quick-deploy.sh`, `scripts/dev/postprovision.sh`, or `az acr build`
to "make sure the change works" — even when the change is plain backend
Python or frontend TypeScript that is fully covered by `pytest` and a host-mode
`uvicorn` / `npm run dev` smoke. Each unnecessary cycle costs 5–10 minutes and
adds zero validation value over Tier 1/2a.

The previous charter §13 told the agent **what to do** (pytest + local smoke)
but did not tell it **what not to do**. This change closes that gap with an
explicit NON-NEGOTIABLE rule and mirrors it as a tripwire in `AGENTS.md`.

## User-facing change

None. Pure policy update. No source / infra / test files touched.

## What changed

### `.github/copilot-instructions.md` §13

Added a new subsection **"Do NOT redeploy for ordinary code changes
(NON-NEGOTIABLE)"** right after "Validation before marking done". It pins the
two conditions both of which must hold before invoking deploy tooling:

1. Change touches sidecar layout, Container App template, terminal toolchain
   (`terminal/Dockerfile*`, `exec_server.py`), or `infra/*.bicep`.
2. The bug genuinely cannot be reproduced in Tier 1 (pytest) or Tier 2a
   (host-mode `fullstack: start`).

When a redeploy *is* warranted, the change note must record which sidecar and
which Tier 2a check was tried and failed.

### `AGENTS.md` tripwires list

Added tripwire #10 mirroring the rule with a short summary and a back-reference
to charter §13.

## Why this is safe

- It does not block any legitimate redeploy — sidecar / terminal / Bicep
  changes still go through `quick-deploy.sh` and `postprovision.sh`.
- It does not relax any existing safety rule (Storage isolation, MI usage,
  ttyd loopback binding, etc. all remain unchanged).
- It is consistent with the existing three-tier debug loop documented in
  [scripts/dev/README.md](../../scripts/dev/README.md) and with the host-mode
  `fullstack: start` task wired into `.vscode/tasks.json`.

## Validation

- `grep -n "redeploy for ordinary code changes" .github/copilot-instructions.md AGENTS.md`
  confirms the new section and tripwire are present.
- No code / infra / test changes → no pytest / build / what-if evidence
  required.
