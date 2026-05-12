# Cloud-init: install elastic_blast package metadata reliably

**Date**: 2026-05-12
**Scope**: `scripts/cloud-init/remote-terminal.yaml`,
`api/scripts/cloud-init/remote-terminal.yaml` (deployment mirror)

## Motivation

The first BLAST submission failed at the Warmup step with:

```
File "/home/azureuser/elastic-blast-azure/src/elastic_blast/__init__.py",
line 27, in <module>
    VERSION = metadata.version(__package__)
PackageNotFoundError: No package metadata was found for elastic_blast
```

`bin/elastic-blast` calls `importlib.metadata.version("elastic_blast")`,
which only succeeds when the package is `pip install`-ed (so the
`.dist-info/METADATA` file exists). The previous cloud-init tried:

```bash
python setup.py sdist bdist_wheel >/dev/null 2>&1 \
  && pip install --no-deps --force-reinstall dist/elastic_blast-*.whl
```

`>/dev/null 2>&1` swallowed the failure, leaving the venv with
runtime deps but **no package metadata**. The wheel was never
written to `dist/`. SSH inspection confirmed both the missing
`dist/` directory and `pip show elastic_blast` returning "Package(s)
not found".

The build chain is fragile because the upstream `setup.py` uses
NCBI's `packit` extension, which depends on `pbr`, which imports
`pkg_resources`. Modern PEP 517 build isolation strips
`pkg_resources` from the sandbox, so naive `pip install` and
`pip install -e .` both fail. `pip install -e .` additionally
re-invokes pip with `--use-pep517` from inside `setuptools.develop`,
making `--no-build-isolation` insufficient on its own.

## User-facing change

A freshly provisioned Remote Terminal can now run
`./venv/bin/python bin/elastic-blast …` directly, and BLAST
submissions advance past the Warmup step.

## API / IaC diff summary

`scripts/cloud-init/remote-terminal.yaml` (and its mirror
`api/scripts/cloud-init/remote-terminal.yaml`):

- Replaced the `setup.py sdist bdist_wheel + pip install dist/*.whl`
  block with a single deterministic install:

  ```bash
  pip install --no-build-isolation --no-deps \
    /home/azureuser/elastic-blast-azure
  ```

  - `--no-build-isolation` keeps `pkg_resources` available to `pbr`.
  - Non-editable install (no `-e`) avoids `setuptools.develop`'s
    nested `--use-pep517` pip call.
  - `--no-deps` matches the previous behaviour (runtime deps were
    installed in the preceding step from `requirements/test.txt`).
  - Errors are NOT silenced. Failures surface to cloud-init's exit
    status so the orchestrator's `check_cloud_init_activity` can
    detect them.

## Validation evidence

- Reproduced the original `PackageNotFoundError` on the live VM
  (`vm-elb-terminal` in `rg-elb-demo-terminal`).
- Verified the new install command on the same VM:
  `pip install --no-build-isolation --no-deps .` → "Successfully
  installed elastic_blast-0.0".
- `bin/elastic-blast --help` runs and prints the usage banner.
- `pytest -q api/tests/` → 13 passed.
- Function App redeployed via `WEBSITE_RUN_FROM_PACKAGE` user-
  delegation SAS (`funcapp-elbinstall.zip`); restart + `/api/health`
  200.
- The existing VM was unblocked manually with the same command, so
  the in-flight BLAST submission can be retried immediately.

## Cosmetic note (not a code change)

The user observed "Open Storage" appearing to run a second time
during BLAST submission. The orchestrator only emits the
`enabling_storage` phase once; the second `set_storage_public_access_activity`
call (cleanup on failure) does not update `set_custom_status`. The
likely source of confusion is the warmup OUTPUT containing the line
`Enable succeeded: …` (printed by `elastic-blast prepare` when it
toggles storage on its own), which appeared in the failed-job log
panel. With the warmup step now succeeding, this output line is no
longer surfaced as the prominent failure message.
