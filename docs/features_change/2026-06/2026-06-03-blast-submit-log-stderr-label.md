---
title: BLAST submit live log no longer mislabels progress as "[stderr]"
description: ElasticBLAST routes its full INFO log to stderr by design, so the dashboard stopped prefixing those normal progress lines with [stderr].
tags:
  - blast
  - ui
---

# BLAST submit live log — stop mislabeling progress as `[stderr]`

## Motivation

When watching a BLAST **submit** job, every live progress line (query
splitting, workfile upload, "Submitting N jobs to cluster") rendered with a
`[stderr]` prefix, which looked like an error even though the submit was
succeeding.

Root cause: ElasticBLAST is intentionally launched with
`--logfile stderr` (see
[api/tasks/blast/cli_parsing.py](../../../api/tasks/blast/cli_parsing.py)
`_elastic_blast_argv`) so the dashboard captures its full INFO progress log
line-by-line. As a result virtually **every** line arrives on the stderr
stream. The frontend then blindly prefixed any stderr line with `[stderr] `,
turning normal output into something that reads like a failure.

## User-facing change

- Live submit logs from the elastic-blast CLI (`source: "terminal_exec"`) now
  render verbatim — no misleading `[stderr]` prefix. The CLI's own
  `ERROR:` / `WARNING:` level tokens still surface in the line text, so genuine
  errors remain visible.
- Kubernetes pod logs keep their `[pod/container]` prefix (useful source
  attribution).
- A `[stderr]` marker is only kept for some other (non elastic-blast, non-k8s)
  source that emits a genuine separate stderr stream.

## Code change summary

- [web/src/components/BlastStepTimeline/StepLogSection.tsx](../../../web/src/components/BlastStepTimeline/StepLogSection.tsx):
  extracted the inline prefix logic into a new exported, source-aware
  `formatLiveLogLine(event)` helper and used it in the per-phase live-log
  grouping.
- [web/src/components/BlastStepTimeline/StepLogSection.test.ts](../../../web/src/components/BlastStepTimeline/StepLogSection.test.ts):
  added unit tests for `formatLiveLogLine` covering terminal_exec stderr/stdout,
  k8s pod prefix (with/without container), and the unknown-source fallback.

No backend / API / IaC changes.

## Validation evidence

- `cd web && npm test -- --run StepLogSection` → 9 passed.
- `cd web && npm run build` → built successfully (type-check clean).
