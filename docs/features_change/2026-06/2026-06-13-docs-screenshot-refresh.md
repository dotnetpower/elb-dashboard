# Documentation screenshot refresh

## Motivation

The product UI advanced to `v0.2.329` with a changed top navigation and
restructured Dashboard planes, so the published documentation screenshots
(`docs/images/screenshots/*.png`) no longer matched the live control plane.
They needed refreshing against the current build.

## User-facing change

Refreshed the canonical screenshot set defined in
`docs/screenshot-capture-manifest.json`, plus the three Dashboard "plane" hero
crops used on the documentation home page:

| Image | Page | Notes |
| --- | --- | --- |
| `dashboard-overview-desktop.png` | `docs/user-guide/dashboard.md` | Full Dashboard with cluster, resource, and sidecar planes |
| `dashboard-mobile.png` | `docs/user-guide/dashboard.md` | 390 px mobile stack |
| `new-search-desktop.png` | `docs/user-guide/new-search.md` | New Search form, all sections |
| `jobs-desktop.png` | `docs/user-guide/jobs.md` | Recent searches list (14 demo jobs) |
| `results-desktop.png` | `docs/user-guide/results.md` | Results header, tabs, descriptions table |
| `terminal-desktop.png` | `docs/user-guide/terminal.md` | Connected browser terminal |
| `api-reference.png` | `docs/user-guide/api-reference.md` | API Reference endpoint groups |
| `dashboard1.png` / `dashboard2.png` / `dashboard3.png` | `docs/index.md` | Cluster / resource / sidecar plane crops |

The desktop capture viewport in the manifest was lowered from `1440x1000` to
`1280x900`: at 1440 px the new Dashboard card grid caps its content column and
leaves a large empty right margin, while 1280 px fills the layout cleanly.

## Redaction

Screenshots were captured from the live deployment and all environment-specific
identifiers were masked in-DOM before each capture, per the manifest redaction
rules:

- Subscription ID → `00000000-0000-0000-0000-000000000000`
- ACR / Storage account names → `elbacr01` / `elbstg01`
- Tenant ID, API client ID → zeroed placeholders
- Signed-in UPN → `demo@contoso.com`
- Container App DNS label and internal load-balancer IP → demo placeholders

No subscription IDs, tenant IDs, UPNs, account names, tokens, or SAS URLs remain
in the committed images.

## Modal / dialog illustrations

The four interactive modal/dialog illustrations were also refreshed in the same
pass:

| Image | Surface | Notes |
| --- | --- | --- |
| `create-aks-cluster.png` | Dashboard → Add Cluster | Provision dialog: workload/system pools, region, resource group, live preflight checks, estimated cost |
| `get-database.png` | Dashboard → BLAST Databases | NCBI catalog with ready/update/get states and Auto warm toggles |
| `taxonomy-filter.png` | New Search → Taxonomy Filter | Filter scope, popular taxa chips, include/exclude mode |
| `api-jobs-submit.png` | API Reference → `POST /v1/jobs` | Expanded endpoint card with response codes 202/400/401/409/422/429/500 and the Try It request body |

The app's glass modals are rendered with a composite transform that offsets
Playwright's `page.screenshot` capture. They were captured by neutralizing the
transformed ancestors (for the endpoint card, reparenting it directly under
`body`), expanding inner scroll containers, then cropping to the measured
element bounds. Modal content carries no tenant-specific identifiers (resource
group / region / SKU / sample JSON only).

## Validation

- Visual review of every refreshed PNG confirmed redaction and a populated,
  non-empty state.
- `uv run python scripts/docs/check_frontmatter.py` — docs frontmatter guard passes.
- `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict` — strict build succeeds.
