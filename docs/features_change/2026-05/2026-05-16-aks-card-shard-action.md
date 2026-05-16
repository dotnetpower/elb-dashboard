# AKS card — actually run sharding from the dashboard

## Motivation

After making the chip-state difference visible (downloaded → sharded →
warming → ready), the obvious next step was to make the dashboard
*drive* the transition, not just observe it. Until now there was no
browser path to ask `prepare-db` to upload preset shard layouts for an
already-downloaded BLAST DB; the only way to flip a chip from
`downloaded only` to `sharded · ×N` was to wait for an
`elastic-blast submit` to do it as a side effect.

## User-facing change

A `downloaded only` chip in the AKS card's Databases strip is now a
**clickable button**. Hovering shows `… click to run prepare-db
sharding now`. Clicking it:

1. Renders the chip as a transient `sharding…` (blue, animated spinner).
2. POSTs to the new `/api/blast/databases/{db}/shard` endpoint.
3. On success, invalidates the `["blast-databases", …]` query so every
   place using `useBlastDb` (Storage card included) refetches and the
   chip flips to violet `sharded · ×N` immediately, without waiting for
   the regular 60 s poll.
4. On failure, surfaces a one-line `· sharding failed for {db}: {msg}`
   below the chip strip in the warning color.

`ready` / `warming` / `sharded` chips are read-only spans; only the
`downloaded only` state is actionable.

## API / IaC diff summary

### New backend route — `api/routes/stubs.py`

```
POST /api/blast/databases/{db_name}/shard
body: { subscription_id, resource_group, account_name }
```

Synchronous (shard layouts are tiny manifest + .nal text files; even
`core_nt`'s 8 preset layouts finish in seconds). The handler:

- Validates the four identifiers with the same regexes as
  `/api/storage/prepare-db`.
- Reuses `ensure_local_storage_access(...)` so the call works from a
  developer laptop with `LOCAL_DEBUG_AUTO_OPEN_STORAGE=true` and is a
  no-op inside the Container App (private endpoint path).
- Calls `db_sharding.ensure_shard_sets(cred, account, db_name)` —
  which is idempotent: re-clicking a `sharded` chip would just return
  with `created=0, skipped=N` and not re-upload anything.
- Reads the existing `{db}-metadata.json`, merges
  `sharded`/`shard_sets`/`sharded_at` into it (preserving
  `source_version`, `downloaded_at`, `file_count`, `total_bytes`
  written by `/api/storage/prepare-db`), and writes it back.
- On a metadata write failure, returns `metadata_persisted: false` +
  the error so the caller knows the layouts exist but the dashboard
  won't auto-detect them.

### Parser fix — `api/services/storage_data.py`

`list_databases()` now skips top-level paths matching `^\d+shards$`
(e.g. `1shards/`, `8shards/`). Without this, the very first
`ensure_shard_sets` run would create a `.nal` blob at
`1shards/16S_ribosomal_RNA_shard_00/16S_ribosomal_RNA_shard_00.nal`
that the parser interpreted as a brand-new "DB" called
`16S_ribosomal_RNA_shard_00` — exactly what we observed during this
session before the fix.

### New frontend client — `web/src/api/blast.ts`

`blastApi.shardDatabase(sub, rg, account, dbName)` — returns the
typed `ensure_shard_sets` summary (`shard_sets`, `created`,
`skipped`, `errors`, `metadata_persisted`).

### Chip wiring — `web/src/components/ClusterItem.tsx`

- Imports `useMutation` + `useQueryClient`.
- New `shardMutation` (queryKey-shared invalidation with `useBlastDb`).
- Chip render branches: `<button>` for `downloaded only`,
  `<span>` for everything else. The button uses
  `cursor: pointer; appearance: none; font: inherit; filter:
  brightness(1)` and lifts to `brightness(1.18)` on hover so we don't
  need a per-variant CSS rule.
- Transient `sharding…` state (blue, spinner) while the mutation is
  in flight.

## Validation

- `uv run pytest -q api/tests` → 235 passed.
- `cd web && npm run build` → ✓ built in 6.78 s, no TS errors.
- Browser smoke at `http://127.0.0.1:18080/`:
  1. Loaded dashboard, AKS card showed three `downloaded only` chips.
  2. Clicked `16S_ribosomal_RNA` → chip briefly went to `sharding…` →
     refetch landed → chip turned violet `sharded · ×1`.
  3. Reloaded the page after the parser fix landed → no phantom
     `16S_ribosomal_RNA_shard_00` chip; legend visible; `16S` and
     `18S` (sharded earlier in the session) both render as violet
     `sharded · ×1`; `core_nt` still `downloaded only` and clickable.

## Out of scope (intentional)

- No Celery task. Sharding is a small batch of text-blob writes and
  takes seconds even for `core_nt`; introducing a queue + polling UI
  for a sub-10-second action would have been more friction than value.
  If we ever need to shard hundreds of DBs at once the right answer is
  to convert this endpoint to a 202 + Celery task; the chip's
  `sharding…` transient state and the `metadata_persisted` field
  already shape that future work.
- No "reshard" / "delete shards" buttons. `ensure_shard_sets` is
  idempotent; the existing chip already conveys the result.
