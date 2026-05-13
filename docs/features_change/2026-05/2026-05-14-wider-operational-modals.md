# Wider Operational Modals

## Motivation

The UI typography floor was raised for readability, but several operational modals still used narrow widths that made dense tables, controls, and warmup status feel cramped.

## User-facing change

Operational dialogs now use wider layouts. Cluster details, pod logs, storage database lists, cluster creation, confirmation dialogs, and the resource settings panel have more horizontal room while still respecting small viewport bounds.

## API/IaC diff summary

No API or infrastructure changes. Frontend-only layout changes:

- Raised the shared `.glass-dialog` default width from 400 px to 520 px.
- Added backdrop padding so dialogs keep viewport gutters.
- Expanded the AKS cluster detail modal to 1180 px max width.
- Expanded the cluster diagnostics log modal to 1100 px max width.
- Expanded the storage database list modal to 900 px max width.
- Expanded the AKS provision modal to 760 px max width.
- Expanded the resource settings side panel to 520 px max width.

## Validation evidence

- `npm run build` passed.
- `azd deploy web --no-prompt` deployed the production Static Web App.
- Production browser check opened the AKS cluster detail modal; measured dialog size was 865 px wide in a 913 px viewport.
- Production screenshot captured for the widened AKS cluster detail modal.
