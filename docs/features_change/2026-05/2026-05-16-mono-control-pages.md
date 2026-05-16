# Mono Control Pages

## Motivation

Dashboard, New Search, and Jobs had moved toward a compact JetBrains Mono control-plane style, but Custom DB, Lab Tools, Terminal, and API still used mixed generic page treatments. Continuing with page-specific one-off CSS would make the UI harder to maintain.

## User-facing change

Custom DB, Lab Tools, Terminal, and API now share the same JetBrains Mono control-plane treatment: compact framed headers, mono typography, and tighter operational controls. Page titles now identify the ElasticBLAST surface directly.

## API/IaC diff summary

- Added shared frontend CSS utilities: `mono-page`, `mono-header`, and `mono-tab-groups`.
- Applied the utilities to Custom DB, Lab Tools, Terminal, and API Reference pages.
- No API or IaC changes.

## Validation evidence

- `cd web && npm run build` passed.
- `git --no-pager diff --check -- web/src/theme/glass.css web/src/pages/DatabaseBuilder.tsx web/src/pages/ToolsPage.tsx web/src/pages/RemoteTerminal.tsx web/src/pages/ApiReference.tsx web/src/pages/apiReference/ApiHero.tsx docs/features_change/2026-05/2026-05-16-mono-control-pages.md` passed.
- Browser verification confirmed `/blast/databases/build`, `/tools`, `/terminal`, and `/docs` render the expected ElasticBLAST page titles with `mono-page`, `mono-header`, and JetBrains Mono computed font styles.
