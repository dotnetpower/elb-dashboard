# BLAST database chip — reframe stale warm cache as a warming step, not an error

## Motivation

When a cluster auto-stops or scales, the node-local warm cache (the vmtouch'd
DB shards on each node's local SSD) is discarded and the previously-succeeded
`db-warmup` Jobs are left pinned (`nodeName` is immutable) to nodes that no
longer exist. `k8s_warmup_status` correctly flags the database `Stale`, but the
dashboard rendered that chip with the **orange `warn` tone** (the same treatment
as `failed` / `partial`) and the message *"warm cache is stale and should be
refreshed before sharded throughput runs."* — which reads like an error even
though it is a normal lifecycle state: the cache simply needs warming again.

## User-facing change

The stale warm-cache chip on the cluster Databases strip now reads as part of
the **warming lifecycle** instead of an error:

- Tone changed from the orange `warn` variant to the **accent `loading`
  (warming) tone** — the same colour family as the "warming" legend — with the
  Flame icon and **no spinner** (it is a needed/queued step, not actively
  running).
- Chip label changed from `warm stale` (`warm stale · N versions`) to
  **`re-warm needed`** (`re-warm needed · N versions`).
- Inline message changed from *"warm cache is stale and should be refreshed
  before sharded throughput runs."* to *"node-local warm cache was cleared by a
  cluster stop or scale — re-warm to restore the fast sharded path."* — which
  explains the cause (expected, not a failure) and the action.

The wording is accurate whether or not Auto-warm is enabled: it states the cache
needs re-warming without claiming it is actively warming.

## API / IaC diff summary

Frontend-only, no API/IaC change. The backend warmup `status: "Stale"` contract
is unchanged; only the chip's tone/label/message presentation changed.

- `web/src/components/ClusterItem/DatabaseChipStrip.tsx` — stale branch tone
  (`warn` → `loading`), label (`warm stale` → `re-warm needed`), and the
  `dbChipVisibleStatusMessage` stale message.
- `web/src/components/ClusterItem/DatabaseChipStrip.test.ts` — updated the
  asserted stale message.

## Validation evidence

- `npx vitest run src/components/ClusterItem/DatabaseChipStrip.test.ts` → 2 passed.
- `npx eslint` on the two files → exit 0.
- `npm run build` → built clean (only the pre-existing chunk-size warning).
