# Diagnose & solve problems — Reliability & Availability diagnostics

## Motivation

The Settings → **Diagnose & solve problems** section shipped with only
**Identity and Security**; **Reliability** and **Availability and Performance**
were `Coming soon` placeholders. The Settings slide-over is also too narrow to
render per-resource best-practice findings, and the monitor data plane it would
reuse **degrades open** (empty payload on failure), which would silently turn
"could not check" into a false "no problems found".

This change adds real Reliability and Availability/Performance diagnostics that
check the configured Azure resources against Azure Well-Architected Framework
practices, and moves the experience onto a dedicated `/diagnostics` page.

Design of record: [docs/architecture/diagnostics.md](../../architecture/diagnostics.md).

## User-facing change

- The Settings **Diagnose & solve problems** cards are now **launchers**:
  clicking one closes the panel and opens the dedicated **`/diagnostics/:category`**
  page. Identity and Security graduated out of the narrow panel onto the page.
- New **Reliability** and **Availability and Performance** categories render
  severity-ranked findings grouped by resource (AKS / Storage / ACR / Container
  App / API), with a rollup chip set, per-finding recommendation, and a
  Microsoft Learn best-practice link. `critical`/`warning` expand by default.
- Findings degrade honestly: a fetch failure or a permission denial surfaces as
  an **`indeterminate`** finding plus a page banner ("some checks could not be
  verified … re-run with a higher role"), never a fabricated `ok`/`critical`.
  By-design charter choices (`minReplicas=1`, a stopped cluster under
  Reliability) are `info`, not defects.
- The page is on-demand (one run on entry + an explicit **Re-run**); no auto
  polling. A late response from a previous run/category never overwrites a newer
  one.

## API / IaC diff summary

- **New route** `GET /api/diagnostics/{category}` (`require_caller`, read-only,
  cached 30 s, `fresh=true` to bypass). Registered in
  [api/main.py](../../../api/main.py) before the frontend catch-all.
- **New service** `api/services/diagnostics/` — `models.py` (Finding /
  DiagnosticReport / ResourceSnapshot, closed severity vocabulary), `snapshot.py`
  (per-resource fetch with isolation, bounded per-fetch timeout + run deadline,
  non-blocking executor shutdown), `engine.py` (cached, sanitised report
  assembly + structured run log), `rules/reliability.py` + `rules/availability.py`
  (pure, versioned rule catalogs; k8s EOL facts carry an `as_of` and degrade to
  `info`).
- **New SPA**: `web/src/api/diagnostics.ts` (typed client), 
  `web/src/pages/diagnostics/DiagnosticsPage.tsx` + `diagnosticsModel.ts`. 
  `DiagnosticsSection` converted to a launcher; `IdentitySecurityDetail`
  exported for reuse on the page. Routes `/diagnostics` and
  `/diagnostics/:category` added to [web/src/App.tsx](../../../web/src/App.tsx).
- No IaC change. No new dependency. No SAS token, no Storage network flip, no
  Azure Run Command — fetches reuse the existing `monitoring` service helpers.

## Persona impact (§12a)

- Read-only diagnostic. Permission-denied is classified `indeterminate`, never
  `critical`, so a subscription **Reader** sees "could not verify" instead of a
  false failure. `test_persona_matrix.py` stays green (no scope narrowed, no new
  `require_caller` on an SSE stream — this is a plain GET).

## Validation evidence

- Backend: `uv run pytest -q api/tests` → **2972 passed, 3 skipped**. New:
  `test_diagnostics_rules.py`, `test_diagnostics_availability_rules.py`,
  `test_diagnostics_route.py`, `test_diagnostics_snapshot.py` (golden rules +
  route contract + auth + failure→indeterminate + hung-fetch timeout +
  sidecar-degraded→indeterminate). `test_persona_matrix.py`,
  `test_route_contracts.py` green.
- Backend lint: `uv run ruff check api` → clean.
- Frontend: `cd web && npm run build` clean; `npx vitest run` → **706 passed**
  (incl. `diagnosticsModel.test.ts`); ESLint clean on the new/changed files.
- Docs: `check_frontmatter.py` OK (54 pages); `mkdocs build --strict` succeeds
  with the new `architecture/diagnostics.md` wired into nav.

## Hardening applied per phase (critique loop)

- **Phase 1**: the snapshot executor ran fetches sequentially and blocked on
  unfinished threads at `with`-exit → fixed to submit-all-then-collect with a
  non-blocking `shutdown(wait=False, cancel_futures=True)`; a hung fetch now
  returns a `timeout` snapshot within the per-fetch cap (regression test added).
- **Phase 2**: a Re-run could let a slow earlier response overwrite a newer one,
  and a category switch could flash stale findings → fixed with an in-flight
  signal ref (cancel previous) + clearing the report on category change.
- **Phase 3**: a Redis-unavailable sidecar snapshot returned all-`down`, which
  would have rendered a false `critical` → classified as `unavailable`
  (`indeterminate`) instead (regression test added).
