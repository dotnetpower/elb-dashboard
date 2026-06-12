# Pre-baked prepare-db AKS-fanout image: Azure CLI + a PINNED azcopy 10.28.0.
#
# Why this image exists:
#   The AKS-fanout prepare-db pods previously ran the public
#   `mcr.microsoft.com/azure-cli` image and downloaded azcopy from GitHub on
#   EVERY pod start (the per-pod entrypoint in
#   api/services/k8s/prepare_db_jobs.py bootstraps it with `curl | python3
#   tarfile` when `command -v azcopy` fails). Baking azcopy into the image
#   removes that ~20-30 s per-pod cold start plus the hard runtime dependency
#   on egress to github.com — a reliability win for the large `nt` / `core_nt`
#   downloads that still use this path. The entrypoint auto-detects the
#   pre-installed binary (`command -v azcopy` succeeds) and skips the download,
#   so no pod-script change is needed and the public-image fallback still works
#   when this image is not configured.
#
# Pinning: azcopy is pinned to 10.28.0 to match the entrypoint's pin — the
#   `aka.ms` redirect now serves 10.32.4, which panics on every
#   `--from-to=S3Blob` copy. Bump only after verifying a newer build fixes the
#   S3Blob crash, and bump the entrypoint's `ELB_AZCOPY_URL` default in
#   api/services/k8s/prepare_db_jobs.py in the same change.
#
# Pull path: built into the workload ACR (see scripts/dev/postprovision.sh) and
#   pulled by the AKS kubelet managed identity, which already carries AcrPull on
#   that registry (the same grant elastic-blast job images rely on). The account
#   stays private — no anonymous pull.
ARG BASE_IMAGE=mcr.microsoft.com/azure-cli:2.81.0
FROM ${BASE_IMAGE}

ARG AZCOPY_URL=https://github.com/Azure/azure-storage-azcopy/releases/download/v10.28.0/azcopy_linux_amd64_10.28.0.tar.gz

# Mirror the proven entrypoint bootstrap: curl the pinned tarball, then extract
# the single `azcopy` binary with Python's stdlib tarfile (the azure-cli image
# ships no GNU tar). A `python3 -c` one-liner is used instead of a Dockerfile
# heredoc so the build does not depend on BuildKit being enabled in ACR Tasks.
# Fail the build if the binary is missing or unusable.
RUN set -eux; \
    curl -fsSL --retry 5 --retry-delay 5 --max-time 180 "${AZCOPY_URL}" -o /tmp/azcopy.tgz; \
    python3 -c "import os, shutil, tarfile, sys; t = tarfile.open('/tmp/azcopy.tgz', 'r:gz'); m = next((x for x in t.getmembers() if x.name.endswith('/azcopy') and x.isfile()), None); sys.exit('azcopy binary not found inside tarball') if m is None else None; t.extract(m, '/tmp'); shutil.move('/tmp/' + m.name, '/usr/local/bin/azcopy'); os.chmod('/usr/local/bin/azcopy', 0o755); t.close()"; \
    rm -rf /tmp/azcopy.tgz /tmp/azcopy_linux_*; \
    azcopy --version
