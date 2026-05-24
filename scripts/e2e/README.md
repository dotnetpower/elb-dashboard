# UI E2E Scenarios

Run these through `scripts/dev/e2e-ui.sh` so auth mode, local services, and
headed/headless browser settings are prepared consistently.

```bash
npm --prefix web run e2e:install-browsers
scripts/dev/e2e-ui.sh bypass --headless -- npm --prefix web run e2e:ui
scripts/dev/e2e-ui.sh bypass --headless -- npm --prefix web run e2e:all-safe
scripts/dev/e2e-ui.sh bypass --headless -- npm --prefix web run e2e:dashboard
scripts/dev/e2e-ui.sh bypass --headless -- npm --prefix web run e2e:new-search
scripts/dev/e2e-ui.sh bypass --headless -- npm --prefix web run e2e:api-blast
E2E_ALLOW_BLAST_SUBMIT=1 scripts/dev/e2e-ui.sh bypass --headless -- npm --prefix web run e2e:api-blast
```

The Playwright config is split into four projects so safe scenarios can run in
parallel while real Azure lifecycle work stays serialized:

- `ui-mock`: browser UI scenarios with mocked `/api/*` responses. This is the
  default lane for menu/event coverage and is safe to parallelize.
- `api-smoke`: request-context checks against the local API. Submit remains
  opt-in.
- `mutation-mock`: destructive UI actions with mocked API side effects.
- `azure-lifecycle`: real Azure provisioning / DB / BLAST lifecycle; one worker
  only.

`e2e:all-safe` runs `ui-mock`, `api-smoke`, and `mutation-mock` together. It
intentionally excludes the real Azure lifecycle project. `dashboard-smoke` is
non-destructive and checks that core pages render without client exceptions or
`/api/*` 5xx responses. `new-search-options-matrix` mocks the Azure-backed
endpoints and verifies that representative New Search option changes produce
valid submit payloads. `api-blast-submit-smoke` calls the real API and only
submits a BLAST job when `E2E_ALLOW_BLAST_SUBMIT=1` is present.
When running `api-blast-submit-smoke` in `login` mode instead of dev-bypass,
also provide `E2E_BEARER_TOKEN` because the scenario uses Playwright's API
request context rather than the SPA's MSAL token cache.

## Coverage map

Current `ui-mock` coverage includes layout navigation, Dashboard settings / ACR
build / Storage database manager, Live Wall filtering and pause controls, Recent
search filtering / grouping / navigation, New Search payload options, BLAST
Results analytics tabs and filters, Terminal cockpit/manual controls, API
Reference sidebar / try-it / token controls, Lab Tools tabs, and Custom DB build
form interactions.

Current `mutation-mock` coverage includes AKS stop/delete confirmations, Storage
database download initiation, BLAST job deletion, and Upgrade start / rollback /
escape-hatch copy flows. Keep destructive browser events in this lane unless the
test explicitly provisions real Azure resources under `azure-lifecycle`.

When adding a scenario:

- use `*.ui.spec.ts` for fully mocked browser behavior;
- use `*.mutation.spec.ts` for destructive UI actions whose API effects are
	mocked and asserted through `UiMockState`;
- use `*.api.spec.ts` for direct local API request-context checks;
- use `*.azure.spec.ts` only for cost-guarded real Azure lifecycle work.

## Full Azure lifecycle: core_nt

The `azure-core-nt-lifecycle` scenario is intentionally excluded from ordinary
runs unless explicit cost guards are set. It provisions or starts an AKS
cluster, downloads `core_nt`, builds shard layouts, warms the DB, then submits a
small BLAST smoke job and waits for completion.

```bash
E2E_ALLOW_AZURE_LIFECYCLE=1 \
E2E_CONFIRM_AZURE_COSTS=create-core-nt-shard-warmup-blast \
E2E_AZURE_SUBSCRIPTION_ID=<subscription-id> \
E2E_AZURE_RESOURCE_GROUP=<workload-rg> \
E2E_AZURE_REGION=eastus2 \
E2E_AKS_CLUSTER=<cluster-name> \
E2E_STORAGE_ACCOUNT=<storage-account> \
E2E_ACR_NAME=<acr-name> \
scripts/dev/e2e-ui.sh bypass --fullstack --headless -- \
  npm --prefix web run e2e:azure-core-nt-lifecycle
```

Optional overrides: `E2E_STORAGE_RESOURCE_GROUP`,
`E2E_ACR_RESOURCE_GROUP`, `E2E_NODE_SKU` (default `Standard_E32as_v7`), `E2E_NODE_COUNT`,
`E2E_SYSTEM_VM_SIZE`, `E2E_SYSTEM_NODE_COUNT`, `E2E_SHARDING_MODE`, and the
stage timeout variables `E2E_AKS_PROVISION_TIMEOUT_MS`,
`E2E_CORE_NT_PREPARE_TIMEOUT_MS`, `E2E_CORE_NT_SHARD_TIMEOUT_MS`,
`E2E_CORE_NT_WARMUP_TIMEOUT_MS`, `E2E_CORE_NT_BLAST_TIMEOUT_MS`.
By default the scenario stops AKS during the Storage-only prepare/shard phase
to reduce cost, then starts it again for warmup and BLAST. Set
`E2E_STOP_AKS_DURING_STORAGE_PHASE=0` to keep the cluster running throughout.