# Fix: ncbi/elasticblast-job-submit ACR build context

**Date**: 2026-05-12
**Scope**: `api/services/image_tags.py`, `api/function_app.py`

## Motivation

Every attempt to build `ncbi/elasticblast-job-submit:4.1.0` via the
Dashboard's ACR card failed at Step 14 of the Dockerfile:

```
Step 14/18 : COPY cloud-job-submit-aks.sh /usr/bin/
COPY failed: file not found in build context or excluded by .dockerignore:
stat cloud-job-submit-aks.sh: file does not exist
```

The previous task YAML used the repo root as the docker build context
(`-f docker-job-submit/Dockerfile.azure -t … .`). Most of the Dockerfile's
COPY directives use bare filenames (`COPY cloud-job-submit-aks.sh
/usr/bin/`, `COPY templates/pvc-rwm-aks.yaml.template /templates/`)
that resolve relative to the `docker-job-submit/` subdirectory. With
the repo root as context those filenames did not exist and the build
aborted.

The upstream Makefile builds with `cd docker-job-submit && az acr
build -f Dockerfile.azure --image … .`, i.e. context = the
subdirectory. We had to keep the source upload at the repo root
because the `pre_build_cmd` (`cp -r src/elastic_blast/templates
docker-job-submit/`) needs access to both `src/` and
`docker-job-submit/`, but the docker build step itself must descend
into the subdir.

## User-facing change

- "Build" on the ACR card for `ncbi/elasticblast-job-submit` now
  succeeds. Per-image and "Build All" flows use the same fix.
- No SPA changes required.

## API / IaC diff summary

`api/services/image_tags.py`:

- `IMAGE_BUILD_INFO["ncbi/elasticblast-job-submit"]` gains a new
  `build_context_dir: "docker-job-submit"` field. The `dockerfile`
  field stays as the repo-root-relative path
  (`docker-job-submit/Dockerfile.azure`) because ACR Tasks scans
  dependencies on the upload root before the build step descends.
- Comment expanded to explain the dual constraint (source upload
  must include `src/`, build context must be `docker-job-submit/`).

`api/function_app.py` `build_acr_images`:

- Reads `build_info.get("build_context_dir", ".")` and substitutes
  it as the trailing argument of the `build:` step in the generated
  task YAML. All other images keep `.` as the context and behave
  exactly as before.

## Validation evidence

- Reproduced the original failure on `elbacrdemo` registry: run
  `deb` Failed at the COPY step (4m48s).
- Reproduced the production code path with `EncodedTaskRunRequest`
  + the new YAML (run `def` against `elbacrdemo`): **Succeeded**
  (~3m). Image pushed as
  `elbacrdemo.azurecr.io/ncbi/elasticblast-job-submit:4.1.0`.
- `pytest -q api/tests/` → 13 passed.
- Function App will be redeployed via `WEBSITE_RUN_FROM_PACKAGE`
  user-delegation SAS after this commit lands.
