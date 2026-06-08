# 2026-06-08 — Build-version stamp, workload-node submit gate, OpenAPI jobs error visibility

## Motivation

Three independent issues reported together:

1. **Build-version stamp looked like a commit.** Cloud images bake a
   commit-qualified `APP_VERSION` (`0.2.0-commit.<sha>`). The SPA's
   `formatBuildVersion` split that on `.`, saw four segments, bailed out, and
   rendered `v0.2.0-commit.2d563cd · 2d563cd` — the commit appeared twice and
   the build version was unreadable.
2. **A BLAST submit could be accepted with no workload nodes.** A *running* AKS
   cluster whose workload pool is scaled to 0 passed every pre-flight gate
   (`aks_cluster` only checks the control-plane power state). The
   `elastic-blast` Job then sat `Pending` forever and the queued job row was
   stranded. The only node check (`openapi_ready` → sibling `no_workload_nodes`)
   is skipped when the optional elb-openapi sidecar is not deployed.
3. **OpenAPI `/v1/jobs` errors were swallowed.** `GET /api/blast/jobs` degrades
   to locally-recorded rows when the external `/v1/jobs` plane fails (it returns
   `external_degraded=true`), but the Recent searches page never read that flag,
   so an external-plane outage hid OpenAPI-submitted jobs with no indication.

## User-facing change

- Header / Settings footer now render `v0.2.271 · 2d563cd` (release `A.B`,
  build number, then short commit) even for commit-qualified cloud builds.
- Submitting BLAST against a running cluster with an empty/NotReady workload
  pool is rejected up front (HTTP 409 `blocked_by_preflight`) with a
  `no_workload_nodes` gate and a **Scale up workload pool** action, instead of
  stranding a queued job.
- The Recent searches page shows a non-blocking degraded notice
  ("OpenAPI jobs degraded · …") when the external `/v1/jobs` plane cannot be
  reached, while still listing locally-recorded jobs.

## API / IaC diff summary

- `api/services/blast/submit_gates.py`: new `_gate_workload_nodes` gate (reuses
  `k8s_ready_warmup_node_names`); added to `evaluate_submit_gates`. Reuses the
  existing `no_workload_nodes` / `scale_up_workload_pool` SPA remediation. Skips
  (ok) when the cluster is not Running / unverifiable so it never double-blocks
  with `aks_cluster`; `unknown`+critical only when the cluster is Running but
  the K8s node API errors (so `allow_unverified` can downgrade it).
- `api/routes/blast/jobs.py`: `GET /api/blast/jobs` now also returns
  `external_degraded_message` (additive) alongside `external_degraded` /
  `external_degraded_reason`.
- `web/src/utils/buildVersion.ts` (new): shared `formatBuildVersion` that strips
  any SemVer pre-release/build-metadata suffix before the `A.B.C` split. Both
  `Layout.tsx` and `SettingsPanel.tsx` now import it (local duplicates removed).
- `web/src/pages/BlastJobs/useBlastJobsState.ts` + `BlastJobs.tsx`: surface the
  new `externalDegradedNotice` via the shared `DegradedNotice` component.
- No IaC changes.

## Validation evidence

- `uv run ruff check api` → All checks passed.
- `uv run pytest -q api/tests` → 3103 passed, 3 skipped.
  - New gate tests: `test_workload_nodes_gate_*`,
    `test_evaluate_blocks_when_workload_pool_empty` in
    `api/tests/test_blast_submit_gates.py`.
  - Extended `test_canonical_jobs_list_reports_external_detail_code` asserts
    `external_degraded_message == "bad gateway"`.
- `cd web && npx vitest run` → 734 passed (incl. new
  `src/utils/buildVersion.test.ts`, 5 cases).
- `cd web && npm run build` → built clean.
