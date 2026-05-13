# Typography Accessibility

## Motivation

Dense dashboard controls and metadata labels used 8-11 px text in several places, making the UI harder to read and scan on laptop displays.

## User-facing change

The web UI now uses a 14 px body font, 13 px control font, and 12 px minimum for secondary metadata, badges, captions, refresh timestamps, and small inline labels. Top navigation group labels and the Control Plane subtitle were raised to the same minimum floor.

## API/IaC diff summary

No API or infrastructure changes. Frontend CSS only:

- `web/src/theme/glass.css` adds shared typography tokens, raises common component sizes, and applies an accessibility floor for dense inline microcopy.
- `web/src/components/Layout.css` raises topbar navigation and caption typography.

## Validation evidence

- `npm run build` passed.
- Local dev-bypass browser audit on `http://localhost:8091/` found no visible app text below 12 px: `{ "small": [], "summary": { "12": 8, "14": 18, "15": 1 } }`.
- `azd deploy web --no-prompt` deployed the production Static Web App at `https://kind-coast-0eb698500.7.azurestaticapps.net/`.
- Production browser audit found no visible app text below 12 px: `{ "small": [], "summary": { "12": 203, "13": 24, "14": 81, "15": 1, "20": 1 } }`.
- Production dashboard screenshot captured after deployment.
