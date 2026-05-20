# Submit Database Downloaded-Only List

## Motivation

The BLAST submit database picker repeated disabled `Not downloaded` rows for catalogue entries that were not actually selectable. That made the submit flow noisier than necessary.

## User-facing change

The submit database table now lists only databases that are present in storage and can be selected for a job. Category tabs count downloaded databases only; unavailable catalogue entries are left to the Dashboard Storage flow.

## API/IaC diff summary

No API or IaC changes. Frontend-only update in the BLAST submit database picker.

## Validation evidence

- `npm run build` in `web/`.
- Browser check at `/blast/submit`: the database table shows the selected storage database without disabled `Not downloaded` catalogue rows.