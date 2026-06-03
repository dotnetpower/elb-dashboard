---
title: In-page NCBI Sequence Viewer (SViewer) embed
description: Sequence Detail page now embeds the NCBI Sequence Viewer in-page via NCBI's opt-in JS widget, replacing the deep-link-only card.
tags:
  - user-guide
  - blast
---

# In-page NCBI Sequence Viewer (SViewer) embed

## Motivation

The Sequence Detail page previously offered the NCBI Graphical Sequence Viewer
only as an "Open Sequence Viewer" deep link that left the dashboard in a new
tab. The card's copy claimed NCBI "blocks embedding the Sequence Viewer in
other sites" — that is true for a raw cross-origin `<iframe>` (NCBI serves
`X-Frame-Options: SAMEORIGIN`), but **not** for NCBI's official, CORS-enabled
JavaScript widget API, which is explicitly designed for in-page embedding.
Researchers asked to pan / zoom a hit and inspect tracks without losing their
place in the dashboard.

## User-facing change

On the Sequence Detail page, the "Advanced view (NCBI Sequence Viewer)" card now
renders the viewer **in-page**:

- A **"Load interactive viewer"** primary button. The NCBI script is injected
  and the viewer instantiated only on click — nothing third-party runs on page
  render.
- While loading, a spinner with "Loading the NCBI Sequence Viewer…".
- On success, the live SViewer renders inline (min width 800px, horizontal
  scroll on narrow screens) centred on the BLAST hit range when one is present
  (`?hl_start` / `?hl_stop`), with a marker on the hit.
- A persistent **"Open in new tab"** ghost-button fallback always stays
  available so a CSP block, an NCBI outage, or a degraded tenant never strands
  the researcher. If the widget fails to load, a warning row points the user at
  that fallback.

The embed is keyed by accession, so navigating to a different record resets it
to the not-yet-loaded state.

## API / IaC diff summary

No backend route or Bicep changes. The change is frontend + CSP:

- **New** `web/src/pages/sequence/SViewerEmbed.tsx` — lazy, opt-in widget
  component using NCBI's `sviewer.js` + `SeqView.App` programmatic API. The
  script loader is a module-level singleton (one injection per page); the
  viewer instance is torn down on unmount.
- **Edited** `web/src/pages/sequence/SequenceDetail.tsx` — the deep-link-only
  card body is replaced with `<SViewerEmbed key={accession} … />`; the
  outdated "NCBI blocks embedding" copy is removed.
- **Edited** `web/nginx.conf` (active SPA CSP) — added
  `https://www.ncbi.nlm.nih.gov` to `script-src`, `style-src`, `connect-src`,
  `img-src`; kept the existing `frame-src https://www.ncbi.nlm.nih.gov` and left
  `font-src 'self'` untouched (no evidence the widget pulls fonts cross-origin;
  a CSP violation in the browser console post-deploy would flag it if needed).
  NCBI dynamically loads ExtJS + CSS, fetches track data over CORS, and renders
  track graphics as `<img>`, so each of those origins is required.
- **Edited** `api/app/security_headers.py` (`_DEFAULT_CSP`) — mirrored the same
  NCBI origin additions so a future `STRICT_CSP=true` flip does not break the
  embed. This policy is still default-OFF (charter §12a Rule 4).

## Trade-offs

- **Browser ↔ NCBI direct traffic.** The widget is the first browser-side call
  to NCBI in the dashboard (nuccore summary/GenBank/FASTA are backend-proxied).
  It also sets an `appname`-namespaced cookie (`ElbDashboardSV`) to persist the
  user's track configuration. This is acceptable for public sequence data and
  is opt-in (button click).
- **No `'unsafe-eval'`.** We deliberately did **not** add `'unsafe-eval'` to
  `script-src`, to keep the SPA's script surface tight. If NCBI's ExtJS build
  ever requires runtime eval (XTemplate `new Function`), the symptom is a
  CSP-blocked viewer that drops into the documented error/degraded state; the
  one-line follow-up is to add `'unsafe-eval'` to `script-src` in both CSP
  definitions. Documented as a contingency, not shipped by default.

## Design hardening (self-critique pass)

The first cut passed build + tests but a design-lens critique surfaced several
liveness / leak / observability defects that the mechanical pass cannot see;
they were fixed before shipping:

- **Infinite "loading" on a failed script** — a failed `<script>` tag used to be
  left in the DOM with its promise nulled, so a retry / remount re-attached to a
  dead element whose `load`/`error` event never fires again and hung forever.
  Fixed by removing the dead tag on error.
- **No load timeout** — a stalled NCBI fetch or a `SeqViewOnReady` callback that
  never fires would spin forever. Added a 20s upper bound that drops to the
  error/degraded state.
- **Post-unmount init leak** — the deferred `SeqViewOnReady` init could create a
  viewer *after* the unmount cleanup already ran, leaking an ExtJS instance that
  keeps polling NCBI. Added a `mountedRef` guard so init bails after unmount /
  accession change.
- **Swallowed errors** — failures now `console.warn` with context instead of an
  empty `catch`, so a CSP block is debuggable.
- **No in-page retry** — the error state now offers a **Retry** button (the
  script loader supports a clean retry after the dead-tag fix), in addition to
  the always-present "Open in new tab" fallback.

### Open verification item (post-deploy)

`buildLoadParams` URL-encodes the `tracks` / `mk` values via `URLSearchParams`
(`[` → `%5B`, `|` → `%7C`). If NCBI's `app.load()` expects literal brackets /
pipes, the gene-model track or the hit marker may be ignored. The deep-link
fallback uses the same encoding and works in a URL context, but the programmatic
`app.load()` path must be eyeballed once live — confirm the Sequence + Gene
model tracks render and the hit marker lands on the BLAST range.

## Validation evidence

- `cd web && npm run build` — green (TypeScript strict; `SequenceDetail` chunk
  rebuilt).
- `uv run pytest -q api/tests/test_security_headers.py` — 9 passed (CSP gate
  off-by-default + on-when-strict still green after the `_DEFAULT_CSP` edit).
- `uv run ruff check api` — clean.
- End-to-end embedding (live NCBI widget under the new CSP) is exercised after a
  frontend deploy, because the enabling CSP lives in the nginx sidecar rather
  than the Vite dev server.
