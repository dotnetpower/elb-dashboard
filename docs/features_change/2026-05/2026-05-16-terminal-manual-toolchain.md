# Terminal Manual And Toolchain

## Motivation

The browser terminal is used by researchers who may not be comfortable with Linux shell workflows. The terminal image also lacked several expected command-line utilities for basic file inspection and genomics sequence analysis.

## User-facing change

The Terminal page now includes a collapsible manual with beginner Linux commands, file handling examples, BLAST examples, sequence tool examples, Azure commands, and troubleshooting checks. The terminal sidecar now includes common Linux utilities and bioinformatics tools by default, plus an `elb-tool-versions` helper for checking installed tool versions.

## API/IaC diff summary

- Added a Terminal manual content module and a presentational manual component.
- Wired the manual into the Browser Terminal page without changing terminal WebSocket responsibilities.
- Extended the terminal image with beginner Linux utilities, pinned NCBI BLAST+ 2.17.0, MAFFT, SeqKit, Samtools, BCFTools, BEDTools, FastQC, HMMER, EMBOSS, Clustal Omega, and MUSCLE.
- Added `/usr/local/bin/elb-tool-versions`.
- No API or IaC changes.

## Validation evidence

- `uv run pytest -q api/tests/test_terminal_entrypoint.py api/tests/test_terminal_toolchain.py api/tests/test_terminal_banner.py api/tests/test_terminal_history.py` -> 13 passed.
- `cd web && npm run build` -> TypeScript and Vite production build passed.
- `docker compose --progress=plain -p elb-control-local -f scripts/dev/docker-compose.full.yml build terminal` -> terminal image built successfully.
- Runtime smoke in the rebuilt terminal container confirmed `blastn` 2.17.0+, MAFFT, SeqKit, Samtools, BCFTools, BEDTools, FastQC, HMMER, EMBOSS, Clustal Omega, Azure CLI, kubectl, AzCopy, and `elb-tool-versions` on PATH.
- Browser verification at `/terminal`: manual panel opens, shows Linux Basics / BLAST / Sequence Tools / Azure / Troubleshooting sections, and the terminal remains connected.
