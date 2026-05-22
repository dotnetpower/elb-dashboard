# BLAST DB catalog: mark unsupported entries with a dedicated badge

## Motivation

The BLAST Database modal was showing a generic `Not in current NCBI snapshot`
warning for several catalog entries that NCBI **never** publishes via the
S3 mirror (`ncbi-blast-databases` bucket). Users assumed the snapshot was
mid-publish or temporarily broken and kept retrying — but for these DBs the
S3 prefix will stay empty indefinitely:

| Catalog `value`             | S3 keys (snap `2026-05-09-01-05-02`) | Why |
|-----------------------------|---|-----|
| `tls`                       | 0 | No pre-built BLAST DB anywhere; only raw GenBank flat files. |
| `dbsts`                     | 0 | dbSTS retired by NCBI; only `UniSTS` / `Daily.FASTA`. |
| `gss`                       | 0 | Removed from BLAST v5; only `/blast/db/v4/gss_v4.*.tar.gz`. |
| `htgs`                      | 0 | Removed from BLAST v5; only `/blast/db/v4/htgs_v4.*.tar.gz`. |
| `RefSeq_Gene`               | 0 | Removed from BLAST v5; only `/blast/db/v4/refseqgene_v4.tar.gz`. |
| `est`                       | 0 | Removed from BLAST v5; only `/blast/db/v4/`. |
| `wgs`                       | 0 | Not bulk-distributed (~3 TB); use Entrez / online BLAST. |
| `sra`                       | 0 | Not distributed as a BLAST DB; use SRA Toolkit. |
| `refseq_genomes`            | 0 | Not published as a monolithic BLAST DB. |
| `refseq_reference_genomes`  | 0 | Not published under this name. |

Verified 2026-05-23 by listing `https://ncbi-blast-databases.s3.amazonaws.com?list-type=2&prefix={snap}/{db}`
and `https://ftp.ncbi.nlm.nih.gov/blast/db/` + `/v4/`.

## User-facing change

* `web/src/components/cards/storage/BlastDbRow.tsx` now renders a dedicated
  warning badge for entries flagged `unsupported` in the catalog. The badge
  is a clickable link to the real upstream source (FTP path, repository, or
  SRA Toolkit page) and carries a tooltip explaining why elastic-blast 2.17
  cannot pull it.
* The `Get` button is disabled for unsupported entries; tooltip carries the
  `hint` text instead of the misleading "Not in current NCBI S3 snapshot"
  message.
* The generic `Not in current NCBI snapshot` warning is suppressed for these
  rows — the dedicated badge already explains the real situation.
* Reason labels:
  * `no-prebuilt` → `Not provided as BLAST DB`
  * `v4-only` → `BLAST v4 only (incompatible)`
  * `too-large` → `Not bulk-distributed`

## API / IaC diff summary

No backend, ARM, or Bicep changes. SPA-only:

* `web/src/components/cards/storageDbCatalog.ts` — added
  `BlastDbUnsupported` type + optional `unsupported` field on
  `BlastDbCatalogItem`; tagged the 10 entries above.
* `web/src/components/cards/storage/BlastDbRow.tsx` — render unsupported
  badge, block `Get`, suppress snapshot warning when flagged.
* `web/src/components/cards/storage/BlastDbModal.tsx` — exclude
  `unsupported` entries from `previewNames` so the modal stops firing
  doomed NCBI preview HEADs for them on every open.

## Validation

```bash
cd web
npx tsc --noEmit           # clean
npx eslint src/components/cards/storageDbCatalog.ts \
           src/components/cards/storage/BlastDbRow.tsx \
           src/components/cards/storage/BlastDbModal.tsx   # clean
npm run build              # built in 6.59 s
```

Snapshot probe (evidence for the "0 keys" column above):

```bash
snap=2026-05-09-01-05-02
for db in tls dbsts gss htgs RefSeq_Gene est wgs sra refseq_genomes refseq_reference_genomes; do
  curl -s "https://ncbi-blast-databases.s3.amazonaws.com?list-type=2&prefix=${snap}/${db}&max-keys=1000" \
    | grep -oE '<Key>[^<]+</Key>' | wc -l
done
# → 0 for every entry
```

## Follow-up (not in this change)

The user-reported screenshot also showed `nt` rendered as `Not in current NCBI snapshot`, but the S3 mirror has 1000+ keys under `${snap}/nt` (verified
above). That is a separate display bug — most likely a stale entry in
`api.services.ncbi_catalogue._PREVIEW_CACHE` or the SPA `useDbPreviews`
cache — and is intentionally **not** addressed here. To reproduce live:

```bash
curl -s http://127.0.0.1:8085/api/blast/databases/nt/preview \
  -H "Authorization: Bearer <token>"
```

When `available=false` for `nt` against a snapshot that demonstrably has
keys, file a follow-up issue with the response body + `snapshot` field.
