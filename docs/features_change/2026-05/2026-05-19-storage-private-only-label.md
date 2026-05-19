# Storage Private Only Label

## Motivation

The Storage card showed `publicNetworkAccess: Disabled` as `Disabled`, which read like a warning even though private endpoint-only access is the intended production posture.

## User-facing change

The Storage card now labels the secure steady state as `Private only`. Local-debug warnings also describe the limitation as a local browser session not being able to traverse the private endpoint, instead of implying that disabled public access is a problem.

## API / IaC / deployment diff

- No API contract changes.
- The backend degraded message for local data-plane network denial now uses `Private only` wording.
- No IaC changes.

## Validation

- `npx eslint src/components/cards/storage/StorageMetaGrid.tsx src/components/cards/storage/StorageWarnings.tsx src/components/cards/storage/BlastDbSection.tsx src/components/cards/storage/BlastDbModal.tsx src/components/cards/storage/BlastDbLargeConfirm.tsx src/components/DegradedNotice.tsx --max-warnings 0`
- `uv run ruff check api/services/storage_data.py`
- `npm run build`
- Production quick deploy: `scripts/dev/quick-deploy.sh api private-only-api-20260519045846` and `scripts/dev/quick-deploy.sh frontend private-only-frontend-20260519050441`.
- Live check: `/api/health` returns revision `ca-elb-control--0000074`, the Container App template uses both private-only image tags, and the production `assets/index-SUaPyDei.js` bundle contains `Private only`.