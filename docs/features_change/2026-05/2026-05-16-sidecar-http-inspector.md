# 2026-05-16 — Sidecar HTTP inspector mockups (Variant A, critique-hardened)

## Motivation

The Control Plane sidecar already receives every operator HTTP request, but
the Sidecars card on the dashboard only renders aggregate health. Operators
have no way to see *which* request was slow, *who* called it, or *what*
headers/body were sent — they have to dig through container logs.

We need a per-request HTTP inspector on the sidecar card before wiring it to
the real ring buffer (`api/services/request_metrics.py`). Following the prior
AKS-bento pattern, we first scaffolded three design variants in a static
mockup, picked one, and critique-hardened it through three rounds before
committing to backend work.

## User-facing change

* **New mockup route** `/mockups/sidecar-inspector` registered above the
  frontend catch-all in [web/src/App.tsx](../../../web/src/App.tsx).
* **Three variants** rendered on the same fake fixture
  (`generateFixture(NOW)` with `seededRandom(20260516)`, 80 requests) so the
  interaction patterns can be compared on identical input:
  * **Variant A — Timeline scatter + right drawer.** Classic APM. Dots = one
    request each (x = time, y = log-scaled latency). Click a dot or a row to
    open the detail drawer.
  * **Variant B — Sparkline grid + inline expand.** One row per request,
    in-row expand shows headers/body.
  * **Variant C — Lane swimlane + bottom sheet.** Status-class lanes
    (2xx/3xx/4xx/5xx) with bottom sheet detail.
* **Variant A is the recommendation** and was hardened through three rounds
  of self-critique. The other two variants stay in the mockup for design
  comparison only — they are not wired to anything.

The mockup is **not** wired to any real endpoint. The page header explicitly
calls this out: "None of this is wired to a real endpoint — once a variant is
chosen, backend capture (with header / body redaction + 4 KiB caps) will be
added to `api/services/request_metrics.py`."

### Variant A — what landed after 3 critique rounds

| Surface | Feature |
|--|--|
| Header | `● LIVE` indicator (green pulse when stream active, yellow `PAUSED` when paused), status-code count chips (`N ok · N 3xx · N 4xx · N 5xx`) computed from the visible window, window selector `1m / 5m / 15m`, `⚠ Errors` toggle (4xx+5xx only), compact icon-only Pause/Resume button — all on a single header row |
| Chart | Inline SVG 880×220 scatter on a brighter `rgba(255,255,255,0.07)` plot panel; axis lines + tick marks + rotated axis titles; log-scaled y axis with `10/50/200/1000/2000` ms gridlines; dashed `SLA 2000 ms` line with a `<title>` ("requests above this line breach the 2 s p95 budget"); 5xx dots get a contrasting halo ring so they don't disappear into 2xx green; hover crosshair (vertical + horizontal dashed lines through the hovered dot); HTML tooltip clamped to the viewport (TIP_W=260 / TIP_H=124 with IIFE flipLeft/flipUp); selected dot keeps a ring even after the mouse leaves; empty-window state ("No requests in selected window") |
| Tooltip | Method · status pill · latency (color-graded by `latencyTone(ms)`: success <200ms, primary <500ms, warning <2000ms, danger ≥2000ms) · path · caller · timestamp · "Click point for full request / response" hint |
| Table | Sticky header (z-index 1); rows are `tabIndex=0` with Enter/Space keyboard activation; Duration column color-graded by `latencyTone()`; `ChevronRight` row affordance; `Filter by path, caller, request_id, status code…` search box with `X of Y` counter and clear-X; "Show N more · X hidden" pagination button; rows respect the window selector + errors-only + search filter cascade |
| Drawer | `role="dialog"` with `aria-label`; Esc closes; **Copy as curl** button generates `curl -X METHOD 'https://elb.example.com{path}' -H 'k: v' --data 'body'` from the redacted headers/body; per-section Copy buttons; sections: Request ID / Time / Caller / Client IP / Status·Duration·Size / Request headers (Authorization redacted) / Response headers / Body (4 KiB cap visualised) |

### Critique rounds (what the rubric caught)

* **Round 1 — functional bugs.** Tooltip Y-clipping at top of chart, empty
  fixture crash (`Math.min(...[])` = Infinity), broken rotated `ChevronUp`
  on table rows, missing PAUSED visual when Pause button hit, missing
  status-class count chips, missing Errors-only filter, drawer not
  Esc-closable, drawer missing ARIA role.
* **Round 2 — hardening features.** Window selector (1m/5m/15m), search box
  with filter cascade, 5xx halo for color-blind readability, selected-dot
  ring strengthened, `latencyTone()` applied to both tooltip and table
  Duration column, table "Show more" pagination, keyboard nav on rows,
  chart x-axis anchored to `windowStart/windowEnd` instead of data
  min/max so the time scale is honest when the window is sparse.
* **Round 3 — polish.** `● LIVE` heartbeat indicator with `livePulse`
  keyframe, hover crosshair on chart, `<title>` annotation on the SLA
  reference line, Copy-as-curl button in drawer header, header collapsed
  into a single row by replacing "Errors only / All" with `⚠ Errors` and
  the Pause/Resume label with a 26×22 icon-only button.

## API / IaC diff

None in this change. The mockup is purely a `web/` page that consumes a
deterministic in-memory fixture. The backend wire-up (extending
`api/services/request_metrics.py` `_Sample` to capture headers/body/caller/
client IP/request_id, adding `/api/monitor/sidecar-requests`, replacing the
Variant A fixture with the typed client) is the explicit follow-up.

### Files touched

* `web/src/pages/mockups/SidecarInspectorMockups.tsx` — new (~1600 lines).
  Three variants + shared fixture + Variant A round-3 hardening.
* `web/src/App.tsx` — registers `/mockups/sidecar-inspector` above the
  catch-all redirect.

## Validation evidence

All screenshots captured against `http://127.0.0.1:18080/mockups/sidecar-inspector`
served by the local `elb-control-local` compose project after
`cd web && npm run build` (clean — no TS errors) and a `frontend` sidecar
restart.

| Round | Evidence |
|--|--|
| Round 1 — baseline + Errors+Paused filter (80→1 row) | [sidecar-r1.png](../../temp/sidecar-r1.png) · [sidecar-r1-errors-paused.png](../../temp/sidecar-r1-errors-paused.png) |
| Round 2 — windowed (1m + search filter cascade 80→13→4) | [sidecar-r2.png](../../temp/sidecar-r2.png) · [sidecar-r2-1m-search.png](../../temp/sidecar-r2-1m-search.png) |
| Round 3 — compact header with LIVE indicator | [sidecar-r3.png](../../temp/sidecar-r3.png) (initial 2-row layout caught by self-review) · [sidecar-r3b.png](../../temp/sidecar-r3b.png) (single-row after compaction) |
| Round 3 — hover crosshair + colored tooltip | [sidecar-r3-hover-tooltip.png](../../temp/sidecar-r3-hover-tooltip.png) |
| Round 3 — drawer + Copy-as-curl button | [sidecar-r3-drawer.png](../../temp/sidecar-r3-drawer.png) |

Build artifacts:

* `cd web && npm run build` → clean (`tsc -b && vite build`, ~7 s).
* `get_errors` on `SidecarInspectorMockups.tsx` and `App.tsx` → no
  diagnostics.

Manual interaction checklist verified via playwright snapshot:

* Hover on a 2xx dot shows tooltip with `GET 200 …ms /api/monitor/aks` plus
  crosshair lines through the dot.
* Clicking a table row opens the drawer (role="dialog") with all expected
  sections; the `curl` and `Close request detail (Esc)` buttons render.
* Status count chips reflect the visible window (`72 ok · 7 3xx · 0 4xx · 1 5xx`).
* Window switch `5m → 1m` reduces visible samples (`80 samples` →
  `13 requests`); typing in the search box further narrows to 4.
* `⚠ Errors` toggle plus Pause demonstrates the empty/paused state without
  crashing.

## Scope notes (what is NOT in this change)

* **No backend changes.** `api/services/request_metrics.py` still records
  only `ts / path / status / duration_ms`. Adding header / body / caller /
  client IP / request_id capture (with `Authorization` / `Cookie` /
  `X-Api-Key` / `X-Auth-Token` redaction at capture time and a 4 KiB body
  cap for `application/json` and `text/*` only) is the next change and will
  have its own `/api/monitor/sidecar-requests` route in
  `api/routes/monitor.py`.
* **No real Sidecars card wire-up.** `web/src/components/cards/SidecarsCard/`
  is untouched. Once the backend route lands, the chosen Variant A
  components will be lifted out of the mockup file and replace the existing
  card surface, with the fake fixture swapped for a typed client in
  `web/src/api/endpoints.ts`.
* **Variants B and C** stay in the mockup as design comparison only — no
  hardening pass, no test coverage, will be deleted from the mockup once
  Variant A is integrated.
* **Storage `publicNetworkAccess` invariant** unchanged — this is a
  read-only inspector for in-process request metrics, not a data-plane
  surface.
