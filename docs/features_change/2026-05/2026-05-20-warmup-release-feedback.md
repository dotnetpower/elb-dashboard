# Warmup Release Feedback

## Motivation
Releasing a warm database cache could succeed in the backend while the UI looked inert. The row button only showed the in-flight spinner briefly and there was no durable success, partial-success, or error acknowledgement.

## User-facing change
The DB Warmup panel now shows skeleton rows while database candidates load. Releasing a warm cache now produces an inline status notice plus a toast for success, partial completion, and failure, including deleted-resource and error counts where available.

The BLAST submit Execution Profile section also shows a structured skeleton while AKS clusters load instead of a single inert loading sentence.

## API/IaC diff summary
- `web/src/components/WarmupSection.tsx` adds release pending/success/partial/error state.
- The release mutation now sets a visible pending notice before the API call, refreshes warmup/database queries after completion, and reports the result via toast + inline status.
- Loading candidates now render animated skeleton rows using the existing global skeleton CSS.
- Warmup orchestrator polling now only clears the locally remembered instance id on a confirmed 404, not transient network or server errors.
- `web/src/pages/blastSubmit/ComputeSection.tsx` renders a cluster-card skeleton during execution-profile loading.
- `web/src/App.tsx` adds a route-level skeleton for lazy pages and a terminal-specific unavailable fallback if the lazy terminal bundle fails to load.
- `web/vite.config.ts` pre-bundles the xterm dependencies used by the terminal route, and `web/package.json` removes the unused legacy `xterm` v5 dependency.
- Completed BLAST result transitions no longer look stuck when hits are unavailable or partial: the alignments API now marks `no_result_files` / `storage_unreachable` as degraded, the Descriptions tab uses stable primitive query keys, explains unavailable result files with a retry action, and shows `No pages` instead of `Page 0 / 0` for empty result pages.
- No API or IaC changes.

## Validation evidence
- `cd web && npm run build` → passed.
- Browser check on `http://127.0.0.1:8090/` confirmed dashboard buttons and loading/empty-state affordances are visible; Recent searches recovered from loading to an empty-state action.
- Browser check on `http://127.0.0.1:8090/terminal` after restarting the Vite dev server confirmed the page no longer falls through to the global ErrorBoundary; the terminal cockpit loads normally.
- Browser check on completed job `142d4dd7-b1d9-452f-a849-51ee30601b1a` confirmed `Loading BLAST hits...` clears and the Descriptions tab renders `Results are partial` with parsed file counts (`20 / 31 files`) instead of a stuck loading panel.
- Focused validation: `uv run ruff check api/routes/blast/results.py api/tests/test_blast_results_routes.py`; `PYTHONPATH=$PWD uv run pytest -q api/tests/test_blast_results_routes.py::test_alignments_empty_listing_returns_degraded_no_result_files api/tests/test_blast_results_routes.py::test_alignments_sort_and_page_hits`; `cd web && npm run test -- --run src/pages/blastResults/analytics/blastAnalyticsState.test.ts && npm run build` → passed.
