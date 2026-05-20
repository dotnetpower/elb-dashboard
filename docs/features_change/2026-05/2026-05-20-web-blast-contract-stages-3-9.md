# Web BLAST Contract Stages 3-9

Date: 2026-05-20

## Motivation

After adding the compatibility contract, the control plane needed durable provenance, progress events, canonical result manifests, UI visibility, OpenAPI delivery surfaces, evidence validation, and queue visibility so UI and external clients can reason about Web BLAST-compatible execution consistently.

## User-Facing Change

- Submitted jobs now carry a `provenance` bundle with BLAST version, database evidence, query hash/metadata, options, precision, and compatibility details.
- `/api/blast/jobs/{job_id}/events` returns canonical job events derived from job history.
- `/api/blast/jobs/{job_id}/results` includes a canonical `manifest` that distinguishes available, empty, and degraded result states.
- The BLAST result Files tab displays compatibility, BLAST+ version, and manifest summary chips when metadata is available.
- External API clients can call `/api/v1/elastic-blast/jobs/{job_id}/events` and `/api/v1/elastic-blast/jobs/{job_id}/manifest`.
- Evidence registry tests now fail if a verified Web BLAST search-space entry lacks required provenance metadata.
- `/api/blast/jobs/{job_id}/queue` returns active depth and queue position for queued work.

## API / IaC Diff Summary

- Added `api/services/blast_provenance.py`, `blast_events.py`, `blast_result_manifest.py`, `blast_equivalence_evidence.py`, and `blast_queue.py`.
- Extended BLAST submit payloads and external submit payloads with provenance and canonical request metadata.
- Added events, manifest, and queue routes.
- Extended frontend BLAST API types and the result Files tab summary surface.
- No IaC change.

## Validation Evidence

- Focused backend tests added for provenance, events, manifest, evidence registry, queue snapshot, external events/manifest, and route behavior.
- Frontend build passed after the Files tab and API type updates.
- Final combined validation is recorded in `docs/web-blast-compatibility-implementation-plan.md`.
