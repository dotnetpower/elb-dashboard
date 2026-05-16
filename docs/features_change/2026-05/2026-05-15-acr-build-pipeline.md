# ACR build pipeline — end-to-end working

## Motivation

`api.tasks.acr.build_images` was wired but never produced an image in ACR.
Multiple latent issues had to be fixed in sequence before the four upstream
images (`ncbi/elb`, `ncbi/elasticblast-job-submit`,
`ncbi/elasticblast-query-split`, `elb-openapi`) could be built and pushed via
the Celery worker → ACR Tasks REST flow.

User request: **"ACR 빌드도 되도록 해."**

## User-facing change

The ACR card now backs a real, working build button: clicking
"Build images" enqueues `api.tasks.acr.build_images`, which schedules
`az acr build`-equivalent runs against the registry and returns
`status=scheduled` per image. After ~2 minutes each, all four repositories
appear in ACR with the tags pinned in `api/services/image_tags.py`.

Verified state of `elbacr01`:

```
ncbi/elb                            1.4.0
ncbi/elasticblast-job-submit        4.1.0
ncbi/elasticblast-query-split       0.1.4
elb-openapi                         3.4
```

## API / IaC diff summary

### `api/celery_app.py`

* Added `"api.tasks.acr.*": {"queue": "acr"}` to `task_routes`. Without this,
  `build_images.delay(...)` enqueued onto `default`, where the worker for
  the `acr` queue never picked it up.

### `.vscode/tasks.json`, `scripts/dev/docker-compose.full.yml`, `infra/modules/containerAppControl.bicep`

* Worker `-Q` flag updated from `default,azure,blast,storage` to
  `default,acr,azure,blast,storage` so the worker subscribes to the new
  queue across local-dev (VS Code task), full local stack (compose), and the
  deployed Container App sidecar.

### `api/tasks/acr.py`

* **Pinned ACR build client to `2019-06-01-preview`.**
  `azure-mgmt-containerregistry` 10.3.0 defaults to api-version
  `2023-07-01`, where `RegistriesOperations.begin_schedule_run` no longer
  exists. The build path now opens a dedicated client:

  ```python
  ContainerRegistryManagementClient(cred, subscription_id,
                                    api_version="2019-06-01-preview")
  ```

  and imports `EncodedTaskRunRequest` / `PlatformProperties` from
  `azure.mgmt.containerregistry.v2019_06_01_preview.models`.

* **Single source of truth for build paths.** Removed the previously
  hard-coded `_IMAGE_SOURCES` map (which pointed at upstream NCBI repos with
  the wrong directory layout). The task now reads everything from
  `api/services/image_tags.py` (`IMAGE_TAGS`, `IMAGE_BUILD_INFO`,
  `SOURCE_REPO`).

* **Switched from `DockerBuildRequest` to `EncodedTaskRunRequest` with
  `workingDirectory`.** `DockerBuildRequest` always uses the source-archive
  root as the build context, but every NCBI Dockerfile assumes the build is
  invoked from inside `docker-XXX/`. Mirroring the upstream Makefile
  (`cd docker-XXX && az acr build -f Dockerfile.azure ... .`) is only
  possible by emitting a base64-encoded ACR Tasks v1.1.0 YAML with a
  per-step `workingDirectory`. The new helper `_schedule_acr_build`
  composes:

  ```yaml
  version: v1.1.0
  steps:
    - cmd: <pre_build_cmd, optional>
    - build: -t <registry>/<image>:<tag> -f <dockerfile-basename> .
      workingDirectory: <ctx_dir>     # omitted when ctx_dir == "."
    - push:
        - <registry>/<image>:<tag>
  ```

  This lets each build see exactly the files its Dockerfile expects
  (`requirements.txt`, `entrypoint.sh`, …) without copying source paths
  around.

### `api/services/image_tags.py`

* Switched `ncbi/elb` and `ncbi/elasticblast-query-split` from
  `dockerfile: "Dockerfile"` to `dockerfile: "Dockerfile.azure"` to match
  the upstream Makefiles
  (`docker-blast/Makefile`, `docker-qs/Makefile`), both of which run
  `az acr build -f Dockerfile.azure --registry $(AZURE_REGISTRY) --image
  $(IMG):$(VERSION) .` for Azure builds. The plain `Dockerfile` is the
  GCP/AWS variant; in `docker-qs/` it is based on
  `google/cloud-sdk:alpine`, which now ships Python 3.14 with a broken
  `pkg_resources`, causing `dee` to fail before the .azure switch.
* `elb-openapi` keeps `dockerfile: "Dockerfile"` because no `.azure`
  variant exists upstream; `ncbi/elasticblast-job-submit` was already on
  `Dockerfile.azure`.

## Validation evidence

```
$ uv run pytest -q api/tests
71 passed in 10.57s

$ az acr task list-runs -r elbacr01 --top 6 -o table
RUN ID    STATUS     TRIGGER    DURATION
deh       Succeeded  Manual     00:01:36   # ncbi/elasticblast-query-split:0.1.4 (Dockerfile.azure)
deg       Succeeded  Manual     00:01:36   # ncbi/elb:1.4.0 (Dockerfile.azure)
def       Succeeded  Manual     00:02:53   # elb-openapi:3.4
ded       Succeeded  Manual     00:02:02   # ncbi/elb:1.4.0 (Dockerfile, pre-fix)
dea       Succeeded  Manual     00:02:36   # ncbi/elasticblast-job-submit:4.1.0
dee       Failed     Manual     00:00:33   # query-split with plain Dockerfile (pre-fix)

$ az acr repository list -n elbacr01 -o tsv
elb-openapi
ncbi/elasticblast-job-submit
ncbi/elasticblast-query-split
ncbi/elb
```

End-to-end Celery enqueue → schedule → push log:

```
Task api.tasks.acr.build_images[…] received
ACR build scheduled: ncbi/elb:1.4.0 in elbacr01
ACR build scheduled: ncbi/elasticblast-query-split:0.1.4 in elbacr01
Task api.tasks.acr.build_images[…] succeeded in 2.97s:
  {'results': [{'image': 'ncbi/elb:1.4.0', 'status': 'scheduled'},
               {'image': 'ncbi/elasticblast-query-split:0.1.4', 'status': 'scheduled'}]}
```

## Cross-repo consistency

When `dotnetpower/elastic-blast-azure` bumps any of:

* `docker-blast/Dockerfile.azure`
* `docker-qs/Dockerfile.azure`
* `docker-job-submit/Dockerfile.azure`
* `docker-openapi/Dockerfile`

…or its `azure-prereq.md` step structure, update `IMAGE_TAGS` /
`IMAGE_BUILD_INFO` in [api/services/image_tags.py](../../../api/services/image_tags.py)
and re-run the four ACR builds.
