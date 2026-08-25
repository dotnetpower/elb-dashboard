---
title: Frontend dependency hardening
description: Remove known frontend dependency advisories while preserving routing, authentication, and production bundle behavior.
tags: [security, ui]
---

# Frontend dependency hardening

## Motivation

The production frontend image build reported 13 dependency advisories: one critical, five high, six moderate, and one low. The critical advisory was limited to the test runner, but the production dependency audit also reported three moderate [React Router](https://reactrouter.com/) advisories. Leaving build-time vulnerabilities unresolved would keep the release pipeline exposed even when the affected packages were not copied into the final nginx image.

## User-facing change

There is no intended UI, route, authentication, or API behavior change. The existing React Router v7 future behavior is now provided by Router v7 directly instead of the removed v6 `future` prop. Cloud builds continue to compile with `VITE_AUTH_DEV_BYPASS=false`, so MSAL remains mandatory in the deployed SPA.

## Dependency summary

- `react-router-dom` moves from 6.30.3 to 7.18.2.
- [Vite](https://vite.dev/) moves from 5.4.21 to 7.3.6 and `@vitejs/plugin-react` moves from 4.7.0 to 5.2.0.
- [Vitest](https://vitest.dev/) moves from 2.1.9 to 4.1.11.
- Vulnerable transitive Babel, brace-expansion, js-yaml, nanoid, and PostCSS packages are refreshed through the npm lockfile.
- Vite 8 was evaluated but rejected because its Rolldown transition collapsed the established manual chunk boundaries and increased the `blast-results` chunk from about 373 kB to 1.15 MB. Vite 7 retains the patched dependency graph and the existing Rollup chunk behavior.
- The Playwright UI fixture now mocks BLAST log ticket and event endpoints for fixture-only job IDs, preventing mocked job-detail tests from leaking into the real API and Storage backend.
- No dependency was added, and no backend, Azure RBAC, Storage network, Service Bus, or API contract changed.

## Validation

- `npm --prefix web ci` - clean reproducible install.
- `npm --prefix web audit` - 0 vulnerabilities across 251 packages (previously 13).
- `npm --prefix web audit --omit=dev` - 0 production vulnerabilities (previously 3 moderate).
- `npm --prefix web test -- --run` - 108 files and 978 tests passed.
- `npm --prefix web run lint` - passed with zero warnings.
- `VITE_AUTH_DEV_BYPASS=false npm --prefix web run build` - passed on Vite 7.3.6.
- `scripts/dev/e2e-ui.sh bypass --headless --fullstack -- npm --prefix web run e2e:all-safe` - 43 passed, 6 explicitly guarded live-mutation scenarios skipped.
- Production chunk parity: `blast-results` 373.06 kB, `terminal` 80.61 kB, `vendor-react` 180.44 kB, and main `index` 563.23 kB.
