# Terminal Base Image Cache

## Motivation

Full postprovision builds were dominated by the terminal sidecar. The terminal
image installs Ubuntu packages, Azure CLI, kubectl, azcopy, BLAST+, sequence
analysis tools, and the patched elastic-blast runtime, so rebuilding it for a
small shell-script change made the deployment loop take several minutes.

## User-Facing Change

Terminal deploys now reuse a content-hashed `elb-terminal-base` image for the
stable toolchain layer. Normal terminal quick deploys build only a thin runtime
overlay, while `--rebuild-terminal-base` forces the heavy toolchain rebuild when
those dependencies change.

## API / IaC Diff Summary

- Added `terminal/Dockerfile.base` for the heavy terminal toolchain image.
- Added `terminal/Dockerfile.runtime` for the thin runtime overlay used by Azure
  deploy scripts.
- Added `scripts/dev/terminal-base-image.sh` to compute the base image tag,
  detect whether it already exists in ACR, and build it when needed.
- Updated `scripts/dev/postprovision.sh` so full deploys ensure the terminal
  base image before building the final `elb-terminal` runtime image.
- Updated `scripts/dev/quick-deploy.sh terminal` to build `Dockerfile.runtime`
  against the cached base image and added `--rebuild-terminal-base`.
- Updated `scripts/dev/README.md` with the new terminal deploy behavior.

## Validation Evidence

- `bash -n scripts/dev/terminal-base-image.sh scripts/dev/quick-deploy.sh scripts/dev/postprovision.sh`
- `git diff --check -- terminal/Dockerfile.base terminal/Dockerfile.runtime scripts/dev/terminal-base-image.sh scripts/dev/quick-deploy.sh scripts/dev/postprovision.sh scripts/dev/README.md docs/features_change/2026-05/2026-05-19-terminal-base-image-cache.md`
