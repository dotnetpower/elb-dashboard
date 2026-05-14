# Lab Tools: retire VM wording, gate sidecar tools, surface unimplemented backends

## Motivation

The Custom DB builder and Lab Tools tabs still carried "Remote Terminal VM"
wording from the retired topology, sent `terminal_resource_group` /
`terminal_vm_name` in their request payloads, and had no UI gate around
the still-missing backend routes. Probing the running API showed every
Lab Tool action 404'd:

```
POST /api/blast/cost-estimate    -> 404
POST /api/blast/preprocess       -> 404
POST /api/blast/taxonomy         -> 404
POST /api/blast/primer-design    -> 404
POST /api/blast/databases/build  -> 404
```

That meant users could click "Calculate estimate" / "Build database" /
"Design primers" forever with no feedback beyond a generic 404, and the
docstrings on the page still referenced a VM that no longer exists.

## User-facing change

### Wording / payload cleanup (Item B)
- `web/src/pages/DatabaseBuilder.tsx` — page header and Build-step
  subtitle now say "terminal sidecar" instead of "Remote Terminal VM".
- `web/src/pages/tools/toolsPageModel.tsx` — Primer Design menu blurb
  updated to "Run Primer3 in the terminal sidecar".
- `web/src/pages/tools/ToolTabs.tsx` — `primerApi.design(...)` call no
  longer sends `terminal_resource_group` / `terminal_vm_name`.
- `web/src/pages/tools/ToolLayout.tsx` — `SetupRequired` no longer
  mentions a Remote Terminal VM in the prerequisite list.

### Sidecar gating (Item C)
- `web/src/pages/DatabaseBuilder.tsx` — readiness now includes
  "Terminal sidecar" via `useTerminalSidecarHealth()`; with the sidecar
  unavailable, readiness reads `1/4` (Workspace only) and Build is
  disabled.
- `web/src/pages/tools/ToolTabs.tsx` (PrimerDesignTab) — when the
  sidecar is unhealthy, the tab renders a new `SidecarRequired` empty
  state ("Terminal sidecar unavailable") instead of the form.

### Honest 503 responses (Item A.1)
- `api/routes/stubs.py` — added five POST stub routes that return a
  structured 503 with `code: lab_tool_backend_pending`:
  - `/api/blast/cost-estimate`
  - `/api/blast/preprocess`
  - `/api/blast/taxonomy`
  - `/api/blast/primer-design`
  - `/api/blast/databases/build`
- `web/src/api/client.ts` — `formatApiError()` recognises the
  `lab_tool_backend_pending` code and surfaces the upstream message
  instead of the generic "Function App may be starting up" string.

### Visual "Preview only" banner (Item A.2)
- `web/src/pages/tools/ToolLayout.tsx` — added a `NotImplementedBanner`
  component (small amber strip with `AlertTriangle` icon).
- Rendered at the top of Cost Estimator, Preprocessor, Taxonomy, Primer
  Design (when the sidecar happens to be up), and Custom Database
  Builder so users know the action will fail with `503
  lab_tool_backend_pending` until the Celery tasks land.

## API / IaC diff summary

- `api/routes/stubs.py`: +5 POST routes, all returning HTTP 503 with
  `{"code":"lab_tool_backend_pending","message":"..."}` detail. No
  business logic; no Celery enqueue; no Azure SDK calls.
- No Bicep / `pyproject.toml` / Celery worker changes.
- The optional `terminal_resource_group` / `terminal_vm_name` fields on
  the SPA-side request types remain (other callers may still send them);
  the SPA itself no longer does.

## Validation evidence

```
$ for p in cost-estimate preprocess taxonomy primer-design databases/build; do
    curl -s -o /dev/null -w "POST $p -> %{http_code}\n" \
      -X POST "http://localhost:8080/api/blast/$p" \
      -H 'Content-Type: application/json' -d '{}'
  done
POST cost-estimate -> 503
POST preprocess -> 503
POST taxonomy -> 503
POST primer-design -> 503
POST databases/build -> 503

$ uv run pytest -q api/tests
........................................................                 [100%]
56 passed in 9.54s

$ cd web && npx tsc --noEmit -p . ; echo "exit=$?"
exit=0
```

Browser smoke (local dev, no terminal sidecar):
- `/blast/databases/build`: readiness reads `1/4`, "Build database"
  disabled, amber "Preview only" banner at top, header copy is
  "terminal sidecar".
- `/tools` → Cost Estimator / Preprocessor / Taxonomy: each shows the
  amber "Preview only" banner above the form.
- `/tools` → Primer Design: renders "Terminal sidecar unavailable"
  empty state.

## Files touched

```
api/routes/stubs.py                          (+5 routes)
web/src/api/client.ts                        (formatApiError 503 path)
web/src/pages/DatabaseBuilder.tsx            (readiness + banner + wording)
web/src/pages/tools/ToolLayout.tsx           (SidecarRequired, NotImplementedBanner)
web/src/pages/tools/ToolTabs.tsx             (sidecar gate, banners, payload cleanup)
web/src/pages/tools/toolsPageModel.tsx       (wording)
```
