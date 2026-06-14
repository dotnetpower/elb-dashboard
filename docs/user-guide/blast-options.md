---
title: BLAST Options Reference
description: End-to-end reference of every BLAST execution option the dashboard exposes — the UI form field, the OpenAPI submit field, the BLAST+ CLI flag it maps to, the default value, and the validation rule.
tags:
  - user-guide
  - blast
---

# BLAST Options Reference

This page is the source-of-truth reference for every option that controls a BLAST execution on the ElasticBLAST Control Plane. It lists, for each option:

- The **UI form field** (what you see on the [New Search](new-search.md) page).
- The **OpenAPI submit field** (what `POST /api/blast/submit` accepts — see also the [API Reference](api-reference.md)).
- The **BLAST+ CLI flag** the backend ultimately writes into the elastic-blast options string.
- The **default**, **allowed values**, and **validation rules** enforced before the job is queued.

The reference is grouped by submit-form section so it lines up with what you actually click. Field names and defaults are pulled directly from the code; the linked file:line is the authority if any drift is suspected.

!!! info "Three layers, one option"

    Every option lives in three places: the React form, the OpenAPI payload, and the BLAST+ CLI string that elastic-blast writes into the AKS workload. The dashboard normalises across all three — for example the UI's `low_complexity_filter` boolean, the OpenAPI's `low_complexity_filter` field, and the BLAST+ `-dust yes` flag are the same setting. This page makes the mapping explicit so you can drive the system from any layer.

---

## 1. Program And Query

These fields define **what** you are searching, not how. They are validated at the OpenAPI boundary and cannot be left blank.

| UI field | OpenAPI field | CLI mapping | Default | Notes |
| --- | --- | --- | --- | --- |
| Program | `program` | `[blast] program=` in elastic-blast config | `blastn` | Must match `^(blastn\|blastp\|blastx\|tblastn\|tblastx)$`. Pydantic validator in [api/_http_utils.py](../../api/_http_utils.py#L48). Determines which databases and task presets are valid. |
| Query sequence (FASTA) | `query_data` (inline) **or** `query_blob_url` / `query_file` (Storage blob) | Written to `query.fasta` then `[blast] queries=` | (required) | UI uploads through the api sidecar; the OpenAPI accepts either inline FASTA or a `queries/…` blob reference. Blob references are validated against the `queries` container in [api/_http_utils.py](../../api/_http_utils.py#L69-L84). |
| Query range (subseq) | `query_from`, `query_to` (UI only) | Appended as `-query_loc <from>-<to>` into `additional_options` | (unset) | Optional. Composed by [useSubmitMutation.ts](../../web/src/pages/blastSubmit/useSubmitMutation.ts) at submit time. |
| Job title | `job_title` | (metadata only — not a BLAST flag) | Auto-stamped `YYYYMMDD-hhmm` | UI defaults to an empty string and prepends the timestamp via `buildGeneratedJobTitle` in [blastSubmitModel.ts](../../web/src/pages/blastSubmitModel.ts). |

### Allowed programs

| Program | Direction | Database type | Default `word_size` |
| --- | --- | --- | --- |
| `blastn` | nucleotide → nucleotide | `nucl` | 28 |
| `blastp` | protein → protein | `prot` | 6 |
| `blastx` | translated nucleotide → protein | `prot` | 6 |
| `tblastn` | protein → translated nucleotide | `nucl` | 6 |
| `tblastx` | translated nucleotide → translated nucleotide | `nucl` | 3 |

Source: `PROGRAMS` in [blastSubmitModel.ts](../../web/src/pages/blastSubmitModel.ts#L3-L51).

---

## 2. Search Set (Database)

| UI field | OpenAPI field | CLI mapping | Default | Notes |
| --- | --- | --- | --- | --- |
| Search set | `database` (OpenAPI) / `db` (UI form) | `[blast] db=` | `blast-db/core_nt/core_nt` | Must reference a blob in the `blast-db` container; validated in [api/_http_utils.py](../../api/_http_utils.py#L69-L84). The dropdown lists databases prepared via **BLAST Databases**, scoped to the selected program. |
| `db_total_letters` | `db_total_letters` | `-dbsize` (sharded only) | (auto, from DB metadata) | Pre-populated from `/api/blast/databases`. Used by the backend for `-dbsize` when sharding is enabled but a verified `-searchsp` is not available. |
| `db_total_bytes` | `db_total_bytes` | (not a BLAST flag) | (auto) | Used for shard layout / placement only. |
| `db_effective_search_space` | `db_effective_search_space` | `-searchsp <value>` | (auto, if calibrated) | Only emitted when a verified Web-equivalent search space is recorded for the chosen database. See [Web BLAST search-space discovery](../research/blast-searchsp-discovery.md) for how this value is calibrated. |
| `db_sharded` | `db_sharded` | (controls shard layout selection) | (auto, from DB metadata) | True when the prepared database carries pre-built shard manifests. |

!!! warning "Database paths are not free-form"

    The OpenAPI submit endpoint rejects any `database` that is not a valid `blast-db/<name>/<name>` blob reference on the workspace storage account. This is enforced by `validate_storage_blob_reference` in [api/services/blast/task_config.py](../../api/services/blast/task_config.py). Do not URL-encode the path or pass a full `https://` SAS URL — those are explicitly rejected.

---

## 3. Taxonomy Filter

| UI field | OpenAPI field | CLI mapping | Default | Notes |
| --- | --- | --- | --- | --- |
| Taxonomy filter taxid | `taxid` | `-taxids <id>` **or** `-negative_taxids <id>` | (unset) | Integer NCBI tax id. Pydantic-validated as a positive integer in the form via `parsePositiveTaxid` in [blastSubmitModel.ts](../../web/src/pages/blastSubmitModel.ts). |
| Inclusive / Exclusive | `is_inclusive` | Picks `-taxids` (true) vs `-negative_taxids` (false) | `true` | Only meaningful when `taxid` is set. Composed into the options string in [api/services/blast/config.py](../../api/services/blast/config.py#L270-L275). |
| Lineage label / rank | `taxid_label`, `taxid_rank` (UI cache) | (not a BLAST flag) | (none) | Cached for the lineage preview only; not sent to BLAST. |

### Mutual-exclusivity rule

If you put a structured taxonomy flag (`-taxids` or `-negative_taxids`) into **Additional options** *and* set the **Taxonomy filter** field, the form blocks submit. The conflict check is `hasStructuredTaxidOptionConflict` in [blastSubmitModel.ts](../../web/src/pages/blastSubmitModel.ts).

---

## 4. Task Profile (UI presets)

Task presets pre-fill `word_size` and `evalue` for `blastn` only. Other programs do not show this section; if you need their task variants (`blastp-fast`, `tblastn-fast`, `blastn-short`, …) you pass them through **Additional options** as `-task <name>`.

Source: `BLASTN_OPTIMIZE` in [blastSubmitModel.ts](../../web/src/pages/blastSubmitModel.ts#L53-L80).

| Preset | UI value (`optimize`) | `word_size` | `evalue` | When to choose |
| --- | --- | --- | --- | --- |
| Highly similar sequences (megablast) | `megablast` | 28 | 0.05 | Intra-species comparisons. Default. |
| More dissimilar sequences (discontiguous megablast) | `dc-megablast` | 11 | 0.05 | Cross-species searches. |
| Somewhat similar sequences (blastn) | `blastn` | 7 | 0.05 | Inter-species comparisons, more sensitive but slower. |

!!! note "Short query auto-task"

    For `blastn` jobs whose longest query record is ≤ 50 bases, the submit layer auto-appends `-task blastn-short` to `additional_options`. The threshold lives in `shouldUseBlastnShortTask` in [useSubmitMutation.ts](../../web/src/pages/blastSubmit/useSubmitMutation.ts). Toggle it off with the **Adjust short-query parameters** checkbox in *Algorithm parameters*.

---

## 5. Execution Profile (AKS, Sharding, Warmup)

This section controls **how** the search runs on the cluster.

### Cluster selection

| UI field | OpenAPI field | CLI mapping | Default | Notes |
| --- | --- | --- | --- | --- |
| AKS cluster | `aks_cluster_name` (+ derived `cluster_name`, `resource_group`, `region`) | `[cluster] name=`, `[cluster] resource_group=`, `[cluster] region=` | (required) | The selector lists clusters from `/api/aks/clusters`. The workload pool is auto-detected in [computeEnvironment.ts](../../web/src/pages/blastSubmit/computeEnvironment.ts): prefer the pool named `blastpool`, fall back to the first `mode=user` pool. |
| Machine type | `machine_type` | `[cluster] machine-type=` | `Standard_E32s_v5` | Derived from the workload pool. Override only when you know the cluster ships an alternative SKU. |
| Node count | `num_nodes` | `[cluster] num-nodes=` | `3` | Same source as `machine_type`. |
| PD size | `pd_size` | `[cluster] pd-size=` | `1000Gi` | Default in [useSubmitMutation.ts](../../web/src/pages/blastSubmit/useSubmitMutation.ts). |
| Memory request / limit | `mem_request`, `mem_limit` | `[blast] mem-request=`, `[blast] mem-limit=` | `8Gi` / `24Gi` | Same source. |
| Local SSD | `use_local_ssd` | `[cluster] use-local-ssd=` | `true` | Forces the AKS node-local SSD init path used by elastic-blast. |

### Sharding mode

| UI value | OpenAPI `sharding_mode` | Behaviour | Eligibility | Cross-references |
| --- | --- | --- | --- | --- |
| Off | `off` | Single full-database BLAST. Results are bit-exact with NCBI Web BLAST given the same database snapshot and options. | Always. | — |
| Approximate shard | `approximate` | Partitioned search; results are merged but **not** byte-equivalent to the full-DB run. Sets `allow_approximate_sharding: true`. | `outfmt` must be 5 or 6; database must be sharded; user must opt in. | [shardingAvailability.ts](../../web/src/pages/blastSubmit/shardingAvailability.ts) |
| Precise shard (web-equivalent) | `precise` | Partitioned search gated by the precision report so the merged result matches the full-database run. Sets `use_db_order_oracle: true`. | `outfmt` must be 5 or 6; the database must carry a verified `web_blast_searchsp` value; the precision report must report `eligible=true`. | [api/services/sharding_precision.py](../../api/services/sharding_precision.py), [docs/research/blast-searchsp-discovery.md](../research/blast-searchsp-discovery.md) |

The `disable_sharding` boolean is a legacy opt-out kept for older callers. New code paths should set `sharding_mode: "off"` instead.

!!! warning "outfmt and sharding"

    Only `outfmt 5` (XML) and `outfmt 6` (tabular) support cross-shard merging. The backend rejects `sharding_mode != "off"` with any other format in [api/services/blast/config.py](../../api/services/blast/config.py#L290-L310). The dashboard greys out incompatible shard modes when you pick a non-mergeable format.

### Warmup

| UI field | OpenAPI field | CLI mapping | Default | Notes |
| --- | --- | --- | --- | --- |
| Auto warm | `enable_warmup` | Sets `[cluster] reuse=true` + warmup init logic | `false` | When on, the worker pre-stages the database to the node-local SSD before BLAST starts. Already-warm databases skip this step. Feasibility is checked by `/api/blast/warmup/plan` and replayed in preflight. |
| `skip_warmed_ssd_init` | `skip_warmed_ssd_init` | Skips SSD init even with warmup on | `false` | Internal optimisation; OpenAPI-only. |
| `reuse` | `reuse` | `[cluster] reuse=` | (auto) | Set internally when warmup is on. |

!!! note "Warmup wait has a deadline"

    When Auto warm is on but the chosen node is still staging the database, the submit task does not block — it parks the job in the `waiting_for_warmup` phase (status still `running`) and re-enqueues itself every 30 s until the node reports the database is warm. To stop a permanently-stuck warmup (a node that never leaves `Loading`, or a generation marker that never lands) from looping forever, the wait is bounded by a deadline: **45 minutes by default**, overridable with the `BLAST_WARMUP_MAX_WAIT_SECONDS` worker environment variable. If the deadline is exceeded the job transitions to a terminal `failed` state with phase `warmup_not_ready` and `error_code=node_warmup_wait_deadline_exceeded`. The deadline is enforced in [api/tasks/blast/submit_task.py](../../api/tasks/blast/submit_task.py) (`_warmup_max_wait_seconds`). A database that is already warm skips the wait entirely.

---

## 6. Algorithm Parameters

These are the BLAST+ flags. All of them are optional — leaving the field empty causes the backend to fall back to the BLAST+ default for the selected program/task.

### Quick presets

`PRESETS` in [blastSubmitModel.ts](../../web/src/pages/blastSubmitModel.ts#L82-L103) provides four one-click bundles:

| Preset | `evalue` | `max_target_seqs` | Use case |
| --- | --- | --- | --- |
| Quick scan | 10 | 50 | Fast triage. |
| Standard *(default)* | 0.05 | 100 | Day-to-day BLAST runs. |
| Thorough | 1e-5 | 500 | Wide hit harvest. |
| Publication | 1e-10 | 1000 | Stringent cutoff. |

### Per-field reference

| UI field | OpenAPI field | CLI flag | Default | Allowed values | Notes |
| --- | --- | --- | --- | --- | --- |
| E-value | `evalue` | `-evalue` | `0.05` | Any positive float | Validated as a number in the form; not bounded server-side. |
| Max target sequences | `max_target_seqs` | `-max_target_seqs` | `100` | Any positive integer | Same as above. |
| Output format | `outfmt` | `-outfmt` | `5` (XML) | `0, 5, 6, 7, 11, 12, 13, 14, 15, 16, 17` | Sanitised against shell metacharacters in [api/services/blast/config.py](../../api/services/blast/config.py#L283-L288). Only `5` and `6` are compatible with sharding (see §5). |
| Word size | `word_size` | `-word_size` | (program default — see §1) | Positive integer; empty = use default | The empty string keeps the BLAST+ default for the program/task combination. |
| Gap costs | `gap_open`, `gap_extend` | `-gapopen`, `-gapextend` | (BLAST default) | Pairs from `GAP_COST_OPTIONS` in [AlgorithmParametersSection.tsx](../../web/src/pages/blastSubmit/AlgorithmParametersSection.tsx#L22-L30) or custom integer pair | Form select offers `5,2 / 2,2 / 1,2 / 0,2 / 3,1 / 5,1` plus Linear. |
| Match / Mismatch scores | `match_score`, `mismatch_score` (UI only — sent via `additional_options`) | `-reward`, `-penalty` | (BLAST default) | Pairs from `MATCH_MISMATCH_OPTIONS` in [AlgorithmParametersSection.tsx](../../web/src/pages/blastSubmit/AlgorithmParametersSection.tsx#L32-L37) or custom | `blastn` only. |
| Low-complexity filter | `low_complexity_filter` | `-dust yes/no` (+ `-soft_masking false` when true) | `true` (blastn) | boolean | `blastn` only. Canonical mapping `dust ↔ low_complexity_filter` lives in [api/services/blast/submit_payload.py](../../api/services/blast/submit_payload.py#L170-L180). |
| Adjust short-query parameters | `short_query_adjust` (UI only — sent via `additional_options`) | `-task blastn-short` (auto-applied for ≤ 50 base queries) | `true` (blastn) | boolean | Disables the auto short-task heuristic when off. |
| Max matches in query range | `max_matches_in_query_range` (UI only — sent via `additional_options`) | `-culling_limit` | `"0"` (no culling) | Non-negative integer | Sent only when not `0`. |
| Soft-mask lookup table only | `mask_lookup_table_only` (UI only — sent via `additional_options`) | `-soft_masking true` | `false` | boolean | Overrides the implicit `-soft_masking false` that `low_complexity_filter=true` would otherwise emit. |
| Lowercase masking | `mask_lowercase` (UI only — sent via `additional_options`) | `-lcase_masking` | `false` | boolean | Honours lowercase regions in the query as masked. |
| Species repeat filter | `species_repeat_filter`, `repeat_filter_taxid` (UI only — sent via `additional_options`) | `-window_masker_taxid <taxid>` | off; default taxid `9606` (human) | boolean + integer | Only emitted when the toggle is on and the taxid field is non-empty. |
| Additional options | `additional_options` | Appended verbatim after the composed options | `""` | Raw BLAST+ CLI flags | Use for anything not exposed above (`-task`, `-matrix BLOSUM62`, `-comp_based_stats`, …). The backend appends this string as-is in [api/services/blast/config.py](../../api/services/blast/config.py#L355-L360); shell metacharacters in `-outfmt` are still rejected. |

### Composition order

The backend builds the elastic-blast options string in this order — earlier entries can be overridden by later entries in `additional_options`:

1. `-evalue`
2. `-max_target_seqs`
3. `-taxids` / `-negative_taxids`
4. `-outfmt`
5. `-word_size`
6. `-dust yes|no` (+ `-soft_masking false` when filtering)
7. `-gapopen`, `-gapextend`
8. `-searchsp <effective_search_space>` (if calibrated) **or** `-dbsize <db_total_letters>` (for sharded runs only)
9. `additional_options` (verbatim)

Source: `options_parts` in [api/services/blast/config.py](../../api/services/blast/config.py#L251-L360).

---

## 7. Preflight Gates

Before the **Submit search** button enqueues the Celery task, the dashboard calls `POST /api/blast/pre-flight`. The route returns a `checks[]` array; each non-OK entry blocks submit. Source: [api/routes/blast/preflight.py](../../api/routes/blast/preflight.py).

| Check id | What it validates | How to fix |
| --- | --- | --- |
| `exec_token` | Terminal sidecar exec-token secret is configured. | Reprovision the Container App so the `exec-token` Container Apps secret is present. |
| `terminal_sidecar` | The terminal sidecar `/healthz` answers. | Restart the terminal sidecar. |
| `acr_images` | The BLAST runtime images exist in ACR for the active version tag. | Run **ACR · Build** from the Dashboard. |
| `aks_cluster` | The selected AKS cluster exists and is `Running`. | Start the cluster, or pick a Running one. |
| `storage` | The workspace storage account is reachable. | Restore Storage from the Dashboard panel. |
| `database` | The selected database blob exists and is marked ready. | Run **BLAST Databases → Get** or **Update**. |
| `precision` | The sharding precision report is eligible for `sharding_mode: precise`. | Drop to `approximate` or `off`, or recalibrate the database. |
| `compatibility` | The database is recorded as Web BLAST compatible for the chosen options. | Choose a compatible DB / options combination, or document the deviation. |

The submit route re-runs the precision and compatibility gates server-side in [api/routes/blast/submit.py](../../api/routes/blast/submit.py); a UI bypass would still be rejected.

---

## 8. OpenAPI-Only Fields

The following submit fields are accepted by `POST /api/blast/submit` but have no UI control. They are used by automation, the worker itself, or advanced research workflows. Source: [web/src/api/blast.ts](../../web/src/api/blast.ts#L14-L70).

| Field | Type | Purpose |
| --- | --- | --- |
| `query_data` | string (FASTA) | Inline FASTA, alternative to the `queries/` blob reference. |
| `query_blob_url` | string | Full blob URL (alternative to `query_file`). |
| `query_effective_search_spaces` | `int[]` | Per-query effective search space override for mixed-DB experiments. |
| `query_count` | int | Pre-computed FASTA record count. |
| `batch_len` | int | Pre-computed query batch size for splitting. |
| `db_partitions`, `db_partition_prefix` | int / string | Manual shard count / prefix. Overrides auto-shard selection. |
| `tie_order_oracle_accessions`, `tie_order_oracle_text`, `tie_order_oracle_strict` | list / str / bool | Precise-mode tie-breaking oracle. |
| `use_db_order_oracle` | bool | Activates the DB-order oracle; the form sets this automatically when `sharding_mode="precise"`. |
| `acr_resource_group`, `acr_name` | string | Override the ACR location when it lives outside the cluster RG. |
| `terminal_resource_group`, `terminal_vm_name` | string | Legacy VM-terminal fields. Retained for old callers; the VM terminal route returns 410 Gone. |
| `idempotency_key` | string | Stable replay key. Used to derive deterministic job ids in [api/routes/blast/submit.py](../../api/routes/blast/submit.py). |
| `priority` | int (0–100) | Reserved for future queue scheduling. Default `50`. |
| `resource_profile` | string | Reserved for future named resource bundles. Default `"standard"`. |

These fields are intentionally **not** exposed in the form: they are either advanced research knobs (oracle / precision) or values that the dashboard computes automatically from cluster / database metadata.

---

## 9. Cross-References

- [New Search (user guide)](new-search.md) — the form-driven walkthrough that uses every option on this page.
- [API Reference](api-reference.md) — submit/status payload examples.
- [Web BLAST search-space discovery](../research/blast-searchsp-discovery.md) — how `db_effective_search_space` is calibrated and what `precise` sharding requires.
- [Web BLAST compatibility plan](../research/web-blast-compatibility-plan.md) — canonical option mapping and equivalence policy (the source of the `low_complexity_filter ↔ dust` rule).
- Code source-of-truth:
    - UI form state: [web/src/pages/blastSubmitModel.ts](../../web/src/pages/blastSubmitModel.ts)
    - UI submit composition: [web/src/pages/blastSubmit/useSubmitMutation.ts](../../web/src/pages/blastSubmit/useSubmitMutation.ts)
    - UI algorithm fields: [web/src/pages/blastSubmit/AlgorithmParametersSection.tsx](../../web/src/pages/blastSubmit/AlgorithmParametersSection.tsx)
    - OpenAPI request type: [web/src/api/blast.ts](../../web/src/api/blast.ts)
    - OpenAPI Pydantic model: [api/_http_utils.py](../../api/_http_utils.py)
    - Backend INI builder: [api/services/blast/config.py](../../api/services/blast/config.py)
    - Canonical option mapping: [api/services/blast/submit_payload.py](../../api/services/blast/submit_payload.py)
    - Preflight: [api/routes/blast/preflight.py](../../api/routes/blast/preflight.py)
