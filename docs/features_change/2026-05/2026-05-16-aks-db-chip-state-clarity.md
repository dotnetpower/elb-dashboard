# AKS card — Databases chip state clarity

## Motivation

After integrating the per-database chip strip into the AKS card, every chip
rendered with the same orange icon and only a number badge. Users could not
tell whether a DB was:

- merely **downloaded** to storage (no preparation yet),
- **sharded** (preset layouts uploaded by `prepare-db`, ready for an
  `elastic-blast submit`) but not warmed,
- currently **warming** on cluster nodes (db-warmup daemonset rolling out),
- fully **ready** (vmtouch cache hot on every node — zero cold-start), or
- **failed** (db-warmup pods erroring on one or more nodes).

The user explicitly called out that the existing chip "DATABASES" badge
made it ambiguous whether sharding was incomplete, in-progress, or done,
and that the warmup → sharding relationship was opaque.

## User-facing change

`web/src/components/ClusterItem.tsx` — the per-database strip now renders
each chip as `<icon> <db.name> | <stage>` where `<stage>` is one of:

| stage text | color (CSS variant) | meaning |
| --- | --- | --- |
| `downloaded only` | muted/faint | blob exists in storage; `prepare-db` has not run; `elastic-blast` will shard on first submit |
| `sharded · ×N` | violet (`.shard`) | `prepare-db` uploaded N preset layouts; ready for submit; no node-side cache yet |
| `warming · K/T` | accent blue (`.loading`, animated spinner) | `db-warmup` daemonset rolling out — sharding is implicitly complete (warmup references sharded files) |
| `ready · K/T` | success green | vmtouch hot on every node |
| `warmup failed · F/T` | warning orange (`.warn`) | one or more `db-warmup` pods failed |

A small inline legend (`●downloaded ●sharded ●warming ●ready`) sits next
to the **DATABASES** caption so the chip colors are self-documenting.
Tooltip on the legend explains the pipeline:
`download → prepare-db (sharding) → db-warmup daemonset (vmtouch) → ready`.

Per-chip tooltip adds context: e.g. for `sharded` chips it explains the
shard layout count and that node cache is still cold; for `ready` chips it
calls out zero cold-start; for `downloaded only` it says the next step
is implicit on first submit.

## API / IaC diff summary

None. This is a pure SPA presentation change driven entirely by data
already returned by `/api/blast/databases` (`sharded`, `shard_sets`) and
`/api/monitor/aks/warmup-status` (`databases[].status`, `nodes_ready`,
`nodes_failed`, `total_jobs`).

CSS additions in `web/src/theme/glass.css`:

- `.dv3-warmup-chip.faint` — neutral muted variant for downloaded-only.
- `.dv3-warmup-chip.shard` — violet variant for sharded-but-not-warmed.
- `.dv3-warmup-chip .stage` — small in-chip stage label with a
  `border-left: currentColor` separator for visual rhythm.

## Validation

- `cd web && npm run build` → ✓ built in 5.67s, no TS errors.
- Browser smoke at `http://127.0.0.1:18080/` against the local
  docker-compose stack: AKS card renders the new legend strip and three
  chips (`16S_ribosomal_RNA`, `18S_fungal_sequences`, `core_nt`) each
  display `downloaded only` in the muted/faint variant — matching the
  local cluster's actual state (no `prepare-db` runs, no `db-warmup`
  daemonset). Screenshot in this PR.
