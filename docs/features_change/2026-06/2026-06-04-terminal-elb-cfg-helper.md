---
title: elb-cfg config helper and Cockpit Parameter Form
description: A standalone elb-cfg CLI in the terminal sidecar plus a Cockpit form that compose a correct elastic-blast.ini from environment defaults.
tags:
  - terminal
  - blast
---

# elb-cfg config helper and Cockpit Parameter Form

## Motivation

Authoring an `elastic-blast.ini` by hand before `elastic-blast submit` is
error-prone: the section/key layout, the storage-account container expansion,
and the region/RG/ACR defaults all have to line up with what the dashboard
submit path would have produced. Phase 1 gives the browser-terminal user a
helper that fills those in from the environment so a manual run and a dashboard
submit do not diverge.

## User-facing change

- **`elb-cfg` CLI (terminal sidecar):** `elb-cfg --program blastn -o
  ~/elastic-blast.ini` scaffolds a valid INI from environment defaults; bare
  `--queries` / `--results` / `--db` names are expanded into full blob URLs
  under the matching container (`queries` / `results` / `blast-db`).
  `elb-cfg --check <file>` validates an existing INI.
- The emitted config always sets `azure-storage-account-container`, derived with
  the same logic the backend `generate_config()` uses, so the container is never
  silently missing.
- **Cockpit Parameter Form:** a "Config Builder" form in the Terminal Cockpit
  composes the matching `elb-cfg …` command and pushes it into the Command
  Preview, where it flows through the existing risk classification and Insert
  pipeline.

## Implementation

- [terminal/elb_cfg.py](../../../terminal/elb_cfg.py): standalone, stdlib-only
  helper bundled at `/usr/local/bin/elb-cfg`. `_derive_storage_container()`
  mirrors [api/services/blast/config.py](../../../api/services/blast/config.py);
  `build_config()` always sets `azure-storage-account-container`. URL expansion
  never invents a storage account the caller did not supply.
- [web/src/pages/terminal/TerminalCfgForm.tsx](../../../web/src/pages/terminal/TerminalCfgForm.tsx)
  + `buildElbCfgCommand` / `ELB_CFG_FORM_DEFAULTS` in
  [terminalCockpitModel.ts](../../../web/src/pages/terminal/terminalCockpitModel.ts).
- [terminal/entrypoint.sh](../../../terminal/entrypoint.sh): `scaffold_blast_cfg`
  drops an `~/elastic-blast.ini.template` and an `~/examples/` quick-start.
- [terminal/Dockerfile](../../../terminal/Dockerfile) /
  [terminal/Dockerfile.runtime](../../../terminal/Dockerfile.runtime): install
  the helper into the image.

## Validation

- `uv run pytest api/tests/test_elb_cfg_helper.py -m ''` — 15 passed, including a
  parametrized cross-check that the container derivation matches
  `generate_config()`.
- Manual `elb-cfg --print` smoke run.
