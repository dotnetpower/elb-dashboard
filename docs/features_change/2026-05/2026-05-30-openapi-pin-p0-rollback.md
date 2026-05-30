# 2026-05-30 — openapi pin P0 rollback (4.16 → 4.14)

## Motivation

GitHub issue #20 (P0 #1) flagged that
`api/services/image_tags.py` was pinning `elb-openapi: 4.16` even though
that tag has never been built or pushed to ACR
(`acrelbdashboard3abp67bppe`). Verification confirmed:

* ACR `repository show-tags elb-openapi` only returns `4.14`.
* Sibling `~/dev/elastic-blast-azure/docker-openapi/app/main.py` source
  was bumped to `VERSION = "3.7.2"` (commit `f360821`), but no
  `az acr build -t elb-openapi:4.16 …` was ever run.

The next `azd up` or sidecar redeploy in this state would fail with
`ImagePullBackOff: manifest unknown` and brick the Container App
control plane until manually patched.

## User-facing change

* No SPA / route surface change.
* The next deploy or `apply_template` call will now resolve
  `elb-openapi` to the `4.14` image that actually exists in ACR.
* The 3.7.2 critique-fix round (per-IP anonymous bucket, GC of empty
  rate buckets, autoscaler-aware pool name match) is **temporarily
  unavailable** in deployed environments until tag `4.17` (sibling
  `VERSION = "3.7.3"`) is built and the pin is re-bumped.

## API / IaC diff summary

| Surface | Change |
| --- | --- |
| `api/services/image_tags.py` | `elb-openapi` pin `4.16` → `4.14`; added a 2026-05-30 P0 rollback comment block explaining why and pointing to this note. |

No IaC changes. No new dependencies. Storage
`publicNetworkAccess: Disabled` posture unchanged.

## Validation evidence

* `az acr repository show-tags --name acrelbdashboard3abp67bppe --repository elb-openapi -o tsv` → only `4.14`. The other tags referenced in `image_tags.py` comments (`4.15`, `4.16`) do not exist.
* `uv run ruff check api/services/image_tags.py` — clean.
* `uv run pytest -q api/tests/test_openapi_task.py` — passes (mock-only, no live pull).
* No test or fixture pinned against the literal `"4.16"` value (`grep '"4.16"' api/tests/`).

## Rollout order for the eventual re-bump (4.17)

1. Land the sibling critique-fix round on `elastic-blast-azure/master` so
   `docker-openapi/app/main.py` carries `VERSION = "3.7.3"` plus the
   real fixes for the P1 #2 / #3 / P3 #8 / #9 sub-items of issue #20.
2. `az acr build -r acrelbdashboard3abp67bppe -t elb-openapi:4.17 -f ~/dev/elastic-blast-azure/docker-openapi/Dockerfile ~/dev/elastic-blast-azure/docker-openapi` — verify the tag appears in `az acr repository show-tags`.
3. Bump `IMAGE_TAGS["elb-openapi"]` from `4.14` → `4.17` in this repo and
   refresh the comment block.
4. Update the SPA-side pin reference in `docs/features_change/2026-05/2026-05-29-openapi-critique-fixes.md` (the "image tag pin moved" row currently says `4.15 → 4.16`).

Failing to do these in order will re-introduce the same
`ImagePullBackOff` symptom this rollback is meant to prevent.

## Cross-references

* Issue: [#20](https://github.com/dotnetpower/elb-dashboard/issues/20) P0 #1
* Original change note: [2026-05-29-openapi-critique-fixes.md](2026-05-29-openapi-critique-fixes.md)
* Sibling source state: `~/dev/elastic-blast-azure` HEAD `f360821`
