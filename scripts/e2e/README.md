# UI E2E Scenarios

Run these through `scripts/dev/e2e-ui.sh` so auth mode, local services, and
headed/headless browser settings are prepared consistently.

```bash
npm --prefix web run e2e:install-browsers
scripts/dev/e2e-ui.sh bypass --headless -- npm --prefix web run e2e:dashboard
scripts/dev/e2e-ui.sh bypass --headless -- npm --prefix web run e2e:new-search
scripts/dev/e2e-ui.sh bypass --headless -- npm --prefix web run e2e:api-blast
E2E_ALLOW_BLAST_SUBMIT=1 scripts/dev/e2e-ui.sh bypass --headless -- npm --prefix web run e2e:api-blast
```

`dashboard-smoke` is non-destructive and checks that core pages render without
client exceptions or `/api/*` 5xx responses. `new-search-options-matrix` mocks
the Azure-backed endpoints and verifies that representative New Search option
changes produce valid submit payloads. `api-blast-submit-smoke` calls the real
API and only submits a BLAST job when `E2E_ALLOW_BLAST_SUBMIT=1` is present.
When running `api-blast-submit-smoke` in `login` mode instead of dev-bypass,
also provide `E2E_BEARER_TOKEN` because the scenario uses Playwright's API
request context rather than the SPA's MSAL token cache.